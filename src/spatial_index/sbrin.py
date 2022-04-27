import gc
import logging
import math
import multiprocessing
import os
import sys
import time

import numpy as np

sys.path.append('/home/zju/wlj/st-learned-index')

from src.learned_model import TrainedNN
from src.learned_model_simple import TrainedNN as TrainedNN_Simple
from src.spatial_index.common_utils import Region, binary_search_less_max, get_nearest_none, sigmoid, Point, \
    biased_search_almost, biased_search, get_mbr_by_points
from src.spatial_index.geohash_utils import Geohash
from src.spatial_index.spatial_index import SpatialIndex

PAGE_SZIE = 4096
DATA_SIZE = 28  # x + y + geohash + key


# TODO 检索的时候要检索crs
class SBRIN(SpatialIndex):
    def __init__(self, model_path=None, meta=None, history_ranges=None, current_ranges=None):
        super(SBRIN, self).__init__("SBRIN")
        self.geohash_index = None
        self.model_path = model_path
        logging.basicConfig(filename=os.path.join(self.model_path, "log.file"),
                            level=logging.INFO,
                            format="%(asctime)s - %(levelname)s - %(message)s",
                            datefmt="%Y/%m/%d %H:%M:%S %p")
        self.logging = logging.getLogger(self.name)
        # meta page由meta组成
        # version
        # first_cr: 新增：第一个cr的page指针
        # threshold_number: 新增：hr的数据范围，也是hr分裂的索引项数量阈值
        # threshold_length: 新增：hr分裂的geohash长度阈值
        # threshold_err: 新增：hr重新训练model的误差阈值
        # threshold_summary: 新增：cr的数据范围，也是cr统计mbr的索引项数量阈值
        # threshold_merge: 新增：cr合并的cr数量阈值
        # geohash: 新增：对应L = geohash.sum_bits，索引项geohash编码实际长度
        # last_hr: 优化计算所需：最后一个hr的编号
        self.meta = meta
        # history range pages由多个hr分页组成
        # value: 改动：max_length长度的整型geohash
        # length: 新增：geohash的实际length
        # number: 新增：range范围内索引项的数据量
        # model: 新增：learned indices
        # scope: 优化计算所需
        # value_diff: 优化计算所需：下一个hr value - hr value
        self.history_ranges = history_ranges
        # current range pages由多个cr分页组成
        # value: 改动：mbr
        # number: 新增：range范围内索引项的数据量
        self.current_ranges = current_ranges

    def insert_single(self, point):
        # 1. compute geohash from x/y of point
        point.insert(-1, self.meta.geohash.encode(point[0], point[1]))
        # 2. insert into cr
        # 2.1 if last cr is full, update scope of last cr and pop new cr to crs
        last_cr = self.current_ranges[-1]
        if last_cr.number >= self.meta.threshold_summary:
            first_cr_key = (self.meta.last_hr + 1) * self.meta.threshold_number
            last_cr.value = get_mbr_by_points(self.geohash_index[first_cr_key:first_cr_key + last_cr.number])
            self.create_cr()
            last_cr = self.current_ranges[-1]
        # 2.2 insert data in last cr
        last_cr.number += 1
        # 3. append in geohash index
        self.geohash_index.append(tuple(point))
        # 4. merge cr TODO 改成异步
        self.merge_cr()

    def build(self, data_list, threshold_number, data_precision, region, threshold_err,
              threshold_summary, threshold_merge,
              use_threshold, threshold, core, train_step, batch_num, learning_rate, retrain_time_limit,
              thread_pool_size, save_nn, weight):
        """
        构建SBRIN
        1. order data by geohash
        2. build SBRIN
        2.1. init hr
        2.2. quadripartite recursively
        2.3. create meta and hr
        2.4. reconstruct index items
        2.5. create cr
        3. build learned model
        """
        # 1. order data by geohash
        data_list = data_list.tolist()
        # 2. build SBRIN
        start_time = time.time()
        # 2.1. init hr
        n = len(data_list)
        tmp_stack = [(0, 0, n, 0, region)]
        result_list = []
        geohash = Geohash.init_by_precision(data_precision=data_precision, region=region)
        threshold_length = region.get_max_depth_by_region_and_precision(precision=data_precision) * 2
        # 2.2. quadripartite recursively
        while len(tmp_stack):
            cur = tmp_stack.pop(-1)
            if cur[2] > threshold_number and cur[1] < threshold_length:
                child_regions = cur[4].split()
                l_key = cur[3]
                r_key = cur[3] + cur[2] - 1
                tmp_l_key = l_key
                child_list = [None] * 4
                length = cur[1] + 2
                r_bound = cur[0]
                for i in range(4):
                    value = r_bound
                    r_bound = cur[0] + (i + 1 << geohash.sum_bits - length)
                    tmp_r_key = binary_search_less_max(data_list, 2, r_bound, tmp_l_key, r_key)
                    child_list[i] = (value, length, tmp_r_key - tmp_l_key + 1, tmp_l_key, child_regions[i])
                    tmp_l_key = tmp_r_key + 1
                tmp_stack.extend(child_list[::-1])  # 倒着放入init中，保持顺序
            else:
                # 把不需要分裂的hr加入结果list，加入的时候顺序为[左上，右下，左上，右上]的逆序，因为堆栈
                result_list.append(cur)
        # 2.3. create meta and hr
        result_len = len(result_list)
        self.meta = Meta(1, threshold_number, threshold_length, threshold_err, threshold_summary, threshold_merge,
                         max([result[1] for result in result_list]), geohash, result_len - 1)
        region_offset = pow(10, -data_precision - 1)
        self.history_ranges = [HistoryRange(result_list[i][0], result_list[i][1], result_list[i][2], None,
                                            result_list[i][4].up_right_less_region(region_offset),
                                            2 << 50 - result_list[i][1] - 1) for i in range(result_len)]
        # 2.4. reconstruct index items
        result_data_list = []
        for i in range(result_len):
            result_data_list.extend(data_list[result_list[i][3]: result_list[i][3] + result_list[i][2]])
            result_data_list.extend([(0, 0, 0, 0)] * (threshold_number - result_list[i][2]))
        self.geohash_index = result_data_list
        # 2.5. create cr
        self.current_ranges = []
        self.create_cr()
        end_time = time.time()
        self.logging.info("Create SBRIN: %s" % (end_time - start_time))
        # 3. build learned model
        start_time = time.time()
        self.build_nn_multiprocess(use_threshold, threshold, core, train_step, batch_num, learning_rate,
                                   retrain_time_limit, thread_pool_size, save_nn, weight)
        end_time = time.time()
        self.logging.info("Create learned model: %s" % (end_time - start_time))

    def build_nn_multiprocess(self, use_threshold, threshold, core, train_step, batch_num, learning_rate,
                              retrain_time_limit, thread_pool_size, save_nn, weight):
        multiprocessing.set_start_method('spawn', force=True)  # 解决CUDA_ERROR_NOT_INITIALIZED报错
        pool = multiprocessing.Pool(processes=thread_pool_size)
        mp_dict = multiprocessing.Manager().dict()
        for i in range(self.meta.last_hr + 1):
            hr = self.history_ranges[i]
            # 训练数据为左下角点+分区数据+右上角点
            inputs = [j[2] for j in
                      self.geohash_index[i * self.meta.threshold_number:i * self.meta.threshold_number + hr.number]]
            inputs.insert(0, hr.value)
            inputs.append(hr.value + hr.value_diff)
            data_num = hr.number + 2
            labels = list(range(data_num))
            batch_size = 2 ** math.ceil(math.log(data_num / batch_num, 2))
            if batch_size < 1:
                batch_size = 1
            # batch_size = batch_num
            pool.apply_async(self.build_nn,
                             (i, inputs, labels, use_threshold, threshold, core, train_step, batch_size, learning_rate,
                              retrain_time_limit, save_nn, weight, mp_dict))
        pool.close()
        pool.join()
        for (key, value) in mp_dict.items():
            self.history_ranges[key].model = value

    def build_nn(self, model_key, inputs, labels, use_threshold, threshold, core, train_step, batch_size,
                 learning_rate, retrain_time_limit, save_nn, weight, tmp_dict=None):
        if save_nn is False:
            tmp_index = TrainedNN_Simple(self.model_path, model_key, inputs, labels, core, train_step, batch_size,
                                         learning_rate, weight)
        else:
            tmp_index = TrainedNN(self.model_path, str(model_key), inputs, labels, use_threshold, threshold, core,
                                  train_step, batch_size, learning_rate, retrain_time_limit, weight)
        tmp_index.train()
        abstract_index = AbstractNN(tmp_index.weights, len(core) - 2,
                                    math.ceil(tmp_index.min_err), math.ceil(tmp_index.max_err))
        del tmp_index
        gc.collect()
        tmp_dict[model_key] = abstract_index

    def reconstruct_data_old(self):
        """
        把数据存在predict的地方，如果pre已有数据：
        1. pre处数据的geohash==数据本身的geohash，说明数据重复，则找到离pre最近的[pre-maxerr, pre-minerr]范围内的None来存储
        2. pre处数据的geohash!=数据本身的geohash，说明本该属于数据的位置被其他数据占用了，为了保持有序，找None的过程只往一边走
        存在问题：这种重构相当于在存储数据的时候依旧保持数据分布的稀疏性，但是密集的地方后续往往更加密集，导致这些地方的数据存储位置更加紧张
        这个问题往往在大数据量或误差大或分布不均匀的hr更容易出现，即最后"超出边界"的报错
        """
        hr_size = len(self.history_ranges)
        result_data_list = [None] * hr_size * self.meta.threshold_number
        for i in range(hr_size):
            hr = self.history_ranges[i]
            hr.model.output_min = self.meta.threshold_number * i
            hr.model.output_max = self.meta.threshold_number * (i + 1) - 1
            hr_first_key = i * self.meta.threshold_number
            for i in range(hr_first_key, hr_first_key + hr.number):
                pre = hr.model.predict(self.geohash_index[i][2])
                if result_data_list[pre] is None:
                    result_data_list[pre] = self.geohash_index[i]
                else:
                    # 重复数据处理：写入误差范围内离pre最近的None里
                    if result_data_list[pre][2] == self.geohash_index[i][2]:
                        l_bound = max(pre - hr.model.max_err, hr.model.output_min)
                        r_bound = min(pre - hr.model.min_err, hr.model.output_max)
                    else:  # 非重复数据，但是整型部分重复，或被重复数据取代了位置
                        if result_data_list[pre][2] > self.geohash_index[i][2]:
                            l_bound = max(pre - hr.model.max_err, hr.model.output_min)
                            r_bound = pre
                        else:
                            l_bound = pre
                            r_bound = min(pre - hr.model.min_err, hr.model.output_max)
                    key = get_nearest_none(result_data_list, pre, l_bound, r_bound)
                    if key is None:
                        # 超出边界是因为大量的数据相互占用导致误差放大
                        print("超出边界")
                    else:
                        result_data_list[key] = self.geohash_index[i]
        self.geohash_index = result_data_list

    def create_cr(self):
        self.current_ranges.append(CurrentRange(value=None, number=0))

    def merge_cr(self):
        if len(self.current_ranges) > self.meta.threshold_merge:
            # 1. order index items in crs
            old_data_len = self.meta.threshold_merge * self.meta.threshold_summary
            first_old_key = (self.meta.last_hr + 1) * self.meta.threshold_number
            old_data = sorted(self.geohash_index[first_old_key:first_old_key + old_data_len], key=lambda x: x[2])
            # 2. merge index items into hrs
            hr_key = self.binary_search_less_max(old_data[0][2], 2, self.meta.last_hr) + 1
            tmp_l_key = 0
            tmp_r_key = 0
            while tmp_r_key < old_data_len:
                if hr_key <= self.meta.last_hr:
                    if self.history_ranges[hr_key].value > old_data[tmp_r_key][2]:
                        tmp_r_key += 1
                    else:
                        if tmp_r_key - tmp_l_key > 1:
                            self.update_hr(hr_key - 1, old_data[tmp_l_key:tmp_r_key])
                            tmp_l_key = tmp_r_key
                        hr_key += 1
                else:
                    self.update_hr(hr_key - 1, old_data[tmp_r_key:])
                    break
            # delete crs
            del self.current_ranges[:self.meta.threshold_merge]
            del self.geohash_index[first_old_key:first_old_key + old_data_len]

    def update_hr(self, hr_key, points):
        """
        update hr by points
        """
        points_len = len(points)
        hr = self.history_ranges[hr_key]
        # quicksort->merge_sorted_array->sorted => 50:2:1
        # merge_sorted_array(self.geohash_index, 2, hr.key[0], hr.key[1], points)
        offset = hr_key * self.meta.threshold_number
        points.extend(self.geohash_index[offset:offset + hr.number])
        points = sorted(points, key=lambda x: x[2])
        if points_len + hr.number > self.meta.threshold_number and hr.length < self.meta.threshold_length:
            # split hr
            self.split_hr(hr, hr_key, offset, points)
        else:
            # update hr metadata
            hr.number += points_len
            # TODO: retrain model
            hr.model_update([[point[2]] for point in points])
            # update geohash index
            self.geohash_index[offset:offset + hr.number] = points

    def split_hr(self, hr, hr_key, offset, points):
        # 1. create child hrs
        length = hr.length + 2
        value_diff = hr.value_diff >> 2
        region_offset = pow(10, -self.meta.geohash.data_precision - 1)
        child_regs = hr.scope.up_right_more_region(region_offset).split()
        last_key = len(points) - 1
        tmp_l_key = 0
        child_hrs = [None] * 4
        r_bound = hr.value
        child_geohash_index = []
        for i in range(3):
            value = r_bound
            r_bound = hr.value + (i + 1) * value_diff
            tmp_r_key = binary_search_less_max(points, 2, r_bound, tmp_l_key, last_key)
            number = tmp_r_key - tmp_l_key + 1
            child_geohash_index.extend(points[tmp_l_key: tmp_r_key + 1])
            child_geohash_index.extend([(0, 0, 0, 0)] * (self.meta.threshold_number - number))
            child_hrs[3 - i] = HistoryRange(value, length, number, hr.model,
                                            child_regs[i].up_right_less_region(region_offset), value_diff)
            tmp_l_key = tmp_r_key + 1
        number = last_key - tmp_l_key + 1
        child_geohash_index.extend(points[tmp_l_key:])
        child_geohash_index.extend([(0, 0, 0, 0)] * (self.meta.threshold_number - number))
        child_hrs[0] = HistoryRange(r_bound, length, number, hr.model,
                                    child_regs[i].up_right_less_region(region_offset), value_diff)
        # 2. delete old hr
        del self.history_ranges[hr_key]
        # 3. insert child hrs
        for child_hr in child_hrs:
            self.history_ranges.insert(hr_key, child_hr)
        # 4. update meta
        self.meta.last_hr += 3
        if length > self.meta.max_length:
            self.meta.max_length = length
        # 5. update geohash index
        self.geohash_index = self.geohash_index[:offset] + child_geohash_index + \
                             self.geohash_index[offset + self.meta.threshold_number:]

    def point_query_hr(self, point):
        """
        根据geohash找到所在的hr的key
        1. 计算geohash对应到hr的geohash_int
        2. 找到比geohash_int小的最大值即为geohash所在的hr
        """
        return self.binary_search_less_max(point, 0, self.meta.last_hr)

    def range_query_hr_old(self, point1, point2, window):
        """
        根据geohash1/geohash2找到之间所有hr以及hr和window的相交关系
        1. 使用point_query_hr查找geohash1/geohash2所在hr
        2. 返回hr1和hr2之间的所有hr，以及他们和window的的包含关系
        TODO: intersect函数还可以改进，改为能判断window对于region的上下左右关系
        """
        i = self.binary_search_less_max(point1, 0, self.meta.last_hr)
        j = self.binary_search_less_max(point2, i, self.meta.last_hr)
        if i == j:
            return [((3, None), self.history_ranges[i])]
        else:
            return [(window.intersect(self.history_ranges[k].scope, cross=True), self.history_ranges[k])
                    for k in range(i, j - 1)]

    def range_query_hr(self, point1, point2):
        """
        根据geohash1/geohash2找到之间所有hr的key以及和window的位置关系
        1. 通过geohash_int1/geohash_int2找到window对应的所有org_geohash和对应window的position
        2. 通过前缀匹配过滤org_geohash来找到tgt_geohash
        3. 根据tgt_geohash分组并合并position
        """
        # 1. 通过geohash_int1/geohash_int2找到window对应的所有org_geohash和对应window的position
        hr_key1 = self.binary_search_less_max(point1, 0, self.meta.last_hr)
        hr_key2 = self.binary_search_less_max(point2, hr_key1, self.meta.last_hr)
        if hr_key1 == hr_key2:
            return {hr_key1: 15}
        else:
            org_geohash_list = self.meta.geohash.ranges_by_int(point1, point2, self.meta.max_length)
            # 2. 通过前缀匹配过滤org_geohash来找到tgt_geohash
            # 3. 根据tgt_geohash分组并合并position
            size = len(org_geohash_list) - 1
            i = 1
            tgt_geohash_dict = {hr_key1: org_geohash_list[0][1],
                                hr_key2: org_geohash_list[-1][1]}
            while i < size and hr_key1 <= self.meta.last_hr:
                if self.history_ranges[hr_key1].value > org_geohash_list[i][0]:
                    tgt_geohash_dict[hr_key1 - 1] = tgt_geohash_dict.get(hr_key1 - 1, 0) | org_geohash_list[i][1]
                    i += 1
                else:
                    hr_key1 += 1
            return tgt_geohash_dict
            # 前缀匹配太慢：时间复杂度=O(len(window对应的geohash个数)*(j-i))

    def knn_query_hr(self, point1, point2, point3):
        """
        根据geohash1/geohash2找到之间所有hr的key以及和window的位置关系，并基于和point3距离排序
        1. 通过geohash_int1/geohash_int2找到window对应的所有org_geohash和对应window的position
        2. 通过前缀匹配过滤org_geohash来找到tgt_geohash
        3. 根据tgt_geohash分组并合并position
        4. 计算每个tgt_geohash和point3的距离，并进行降序排序
        """
        # 1. 通过geohash_int1/geohash_int2找到window对应的所有org_geohash和对应window的position
        hr_key1 = self.binary_search_less_max(point1, 0, self.meta.last_hr)
        hr_key2 = self.binary_search_less_max(point2, hr_key1, self.meta.last_hr)
        if hr_key1 == hr_key2:
            return [[hr_key1, 15, 0]]
        else:
            org_geohash_list = self.meta.geohash.ranges_by_int(point1, point2, self.meta.max_length)
            # 2. 通过前缀匹配过滤org_geohash来找到tgt_geohash
            # 3. 根据tgt_geohash分组并合并position
            size = len(org_geohash_list) - 1
            i = 1
            tgt_geohash_dict = {hr_key1: org_geohash_list[0][1],
                                hr_key2: org_geohash_list[-1][1]}
            while i < size and hr_key1 <= self.meta.last_hr:
                if self.history_ranges[hr_key1].value > org_geohash_list[i][0]:
                    tgt_geohash_dict[hr_key1 - 1] = tgt_geohash_dict.get(hr_key1 - 1, 0) | org_geohash_list[i][1]
                    i += 1
                else:
                    hr_key1 += 1
            # 4. 计算每个tgt_geohash和point3的距离，并进行降序排序
            return sorted([[tgt_geohash,
                            tgt_geohash_dict[tgt_geohash],
                            self.history_ranges[tgt_geohash].scope.get_min_distance_pow_by_point_list(point3)]
                           for tgt_geohash in tgt_geohash_dict], key=lambda x: x[2])

    def binary_search_less_max(self, x, left, right):
        """
        二分查找比x小的最大值
        优化: 循环->二分:15->1
        """
        while left <= right:
            mid = (left + right) // 2
            if self.history_ranges[mid].value == x:
                return mid
            elif self.history_ranges[mid].value < x:
                left = mid + 1
            else:
                right = mid - 1
        return right

    def point_query_single(self, point):
        """
        1. compute geohash from x/y of points
        2. find hr within geohash by sbrin.point_query
        3. predict by leaf model
        4. biased search in scope [pre - max_err, pre + min_err]
        """
        # 1. compute geohash from x/y of point
        gh = self.meta.geohash.encode(point[0], point[1])
        # 2. find hr within geohash by sbrin.point_query
        hr_key = self.point_query_hr(gh)
        hr = self.history_ranges[hr_key]
        if hr.number == 0:
            return None
        else:
            # 3. predict by leaf model
            pre = hr.model_predict(gh)
            offset = hr_key * self.meta.threshold_number
            # 4. biased search in scope [pre - max_err, pre + min_err]
            return [self.geohash_index[key][3]
                    for key in biased_search(self.geohash_index, 2, gh, offset + pre,
                                             offset + max(pre - hr.model.max_err, 0),
                                             offset + min(pre - hr.model.min_err, hr.max_key))]

    def range_query_single_old(self, window):
        """
        1. compute geohash from window_left and window_right
        2. get all the hr and its relationship with window between geohash1/geohash2 by sbrin.range_query
        3. for different relation, use different method to handle the points
        3.1 if window contain the hr, add all the items into results
        3.2 if window intersect or within the hr
        3.2.1 get the min_geohash/max_geohash of intersect part
        3.2.2 get the min_key/max_key by nn predict and biased search
        3.2.3 filter all the point of scope[min_key/max_key] by range.contain(point)
        主要耗时间：两次geohash, predict和最后的精确过滤，0.1, 0.1 , 0.6
        # TODO: 由于build sbrin的时候region移动了，导致这里的查询不准确
        """
        region = Region(window[0], window[1], window[2], window[3])
        # 1. compute geohash of window_left and window_right
        gh1 = self.meta.geohash.encode(window[2], window[0])
        gh2 = self.meta.geohash.encode(window[3], window[1])
        # 2. get all the hr and its relationship with window between geohash1/geohash2 by sbrin.range_query
        hr_list = self.range_query_hr_old(gh1, gh2, region)
        result = []
        # 3. for different relation, use different method to handle the points
        for hr in hr_list:
            # 0 2 1 3的顺序是按照频率降序
            if hr[0][0] == 0:  # no relation
                continue
            else:
                if hr[1].number == 0:  # hr is empty
                    continue
                # 3.1 if window contain the hr, add all the items into results
                if hr[0][0] == 2:  # window contain hr
                    result.extend(list(range(hr[1].key[0], hr[1].key[1] + 1)))
                # 3.2 if window intersect or within the hr
                else:
                    # 3.2.1 get the min_geohash/max_geohash of intersect part
                    if hr[0][0] == 1:  # intersect
                        gh1 = self.meta.geohash.encode(hr[0][1].left, hr[0][1].bottom)
                        gh2 = self.meta.geohash.encode(hr[0][1].right, hr[0][1].up)
                    # 3.2.2 get the min_key/max_key by nn predict and biased search
                    pre1 = hr[1].model_predict(gh1)
                    pre2 = hr[1].model_predict(gh2)
                    min_err = hr[1].model.min_err
                    max_err = hr[1].model.max_err
                    l_bound1 = max(pre1 - max_err, hr[1].key[0])
                    r_bound1 = min(pre1 - min_err, hr[1].key[1])
                    key_left = biased_search_almost(self.geohash_index, 0, gh1, pre1, l_bound1, r_bound1)
                    if gh1 == gh2:
                        if len(key_left) > 0:
                            result.extend(key_left)
                    else:
                        key_left = l_bound1 if len(key_left) == 0 else min(key_left)
                        l_bound2 = max(pre2 - max_err, hr[1].key[0])
                        r_bound2 = min(pre2 - min_err, hr[1].key[1])
                        key_right = biased_search_almost(self.geohash_index, 0, gh2, pre2, l_bound2, r_bound2)
                        key_right = r_bound2 if len(key_right) == 0 else max(key_right)
                        # 3.2.3 filter all the point of scope[min_key/max_key] by range.contain(point)
                        result.extend([self.geohash_index[key][3] for key in range(key_left, key_right + 1)
                                       if region.contain_and_border_by_list(self.geohash_index[key])])
        return result

    def range_query_old(self, windows):
        return [self.range_query_single_old(window) for window in windows]

    def range_query_single(self, window):
        """
        1. compute geohash from window_left and window_right
        2. get all relative hrs with key and relationship
        3. get min_geohash and max_geohash of every hr for different relation
        4. predict min_key/max_key by nn
        5. filter all the point of scope[min_key/max_key] by range.contain(point)
        主要耗时间：sbrin.range_query.ranges_by_int/nn predict/精确过滤: 307mil/145mil/359mil
        """
        if window[0] == window[1] and window[2] == window[3]:
            return self.point_query_single([window[2], window[0]])
        # 1. compute geohash of window_left and window_right
        gh1 = self.meta.geohash.encode(window[2], window[0])
        gh2 = self.meta.geohash.encode(window[3], window[1])
        # 2. get all relative hrs with key and relationship
        hr_list = self.range_query_hr(gh1, gh2)
        result = []
        # 3. get min_geohash and max_geohash of every hr for different relation
        position_func_list = [lambda reg: (None, None, None),
                              lambda reg: (  # right
                                  None,
                                  self.meta.geohash.encode(window[3], reg.up),
                                  lambda x: window[3] >= x[0]),
                              lambda reg: (  # left
                                  self.meta.geohash.encode(window[2], reg.bottom),
                                  None,
                                  lambda x: window[2] <= x[0]),
                              lambda reg: (  # left-right
                                  self.meta.geohash.encode(window[2], reg.bottom),
                                  self.meta.geohash.encode(window[3], reg.up),
                                  lambda x: window[2] <= x[0] <= window[3]),
                              lambda reg: (  # up
                                  None,
                                  self.meta.geohash.encode(reg.right, window[1]),
                                  lambda x: window[1] >= x[1]),
                              lambda reg: (  # up-right
                                  None,
                                  gh2,
                                  lambda x: window[3] >= x[0] and window[1] >= x[1]),
                              lambda reg: (  # up-left
                                  self.meta.geohash.encode(window[2], reg.bottom),
                                  self.meta.geohash.encode(reg.right, window[1]),
                                  lambda x: window[2] <= x[0] and window[1] >= x[1]),
                              lambda reg: (  # up-left-right
                                  self.meta.geohash.encode(window[2], reg.bottom),
                                  gh2,
                                  lambda x: window[2] <= x[0] <= window[3] and window[1] >= x[1]),
                              lambda reg: (  # bottom
                                  self.meta.geohash.encode(reg.left, window[0]),
                                  None,
                                  lambda x: window[0] <= x[1]),
                              lambda reg: (  # bottom-right
                                  self.meta.geohash.encode(reg.left, window[0]),
                                  self.meta.geohash.encode(window[3], reg.up),
                                  lambda x: window[3] >= x[0] and window[0] <= x[1]),
                              lambda reg: (  # bottom-left
                                  gh1,
                                  None,
                                  lambda x: window[2] <= x[0] and window[0] <= x[1]),
                              lambda reg: (  # bottom-left-right
                                  gh1,
                                  self.meta.geohash.encode(window[3], reg.up),
                                  lambda x: window[2] <= x[0] <= window[3] and window[0] <= x[1]),
                              lambda reg: (  # bottom-up
                                  self.meta.geohash.encode(reg.left, window[0]),
                                  self.meta.geohash.encode(reg.right, window[1]),
                                  lambda x: window[0] <= x[1] <= window[1]),
                              lambda reg: (  # bottom-up-right
                                  self.meta.geohash.encode(reg.left, window[0]),
                                  gh2,
                                  lambda x: window[3] >= x[0] and window[0] <= x[1] <= window[1]),
                              lambda reg: (  # bottom-up-left
                                  gh1,
                                  self.meta.geohash.encode(reg.right, window[1]),
                                  lambda x: window[2] <= x[0] and window[0] <= x[1] <= window[1]),
                              lambda reg: (  # bottom-up-left-right
                                  gh1,
                                  gh2,
                                  lambda x: window[2] <= x[0] <= window[3] and window[0] <= x[1] <= window[1])]
        for hr_key in hr_list:
            hr = self.history_ranges[hr_key]
            if hr.number == 0:  # hr is empty
                continue
            position = hr_list[hr_key]
            offset = hr_key * self.meta.threshold_number
            key_left = offset
            key_right = key_left + hr.max_key
            if position == 0:  # window contain hr
                result.extend(list(range(key_left, key_right + 1)))
            else:
                # if-elif-else->lambda, 30->4
                gh_new1, gh_new2, compare_func = position_func_list[position](hr.scope)
                # 4 predict min_key/max_key by nn
                if gh_new1:
                    pre1 = hr.model_predict(gh_new1)
                    l_bound1 = max(pre1 - hr.model.max_err, 0)
                    r_bound1 = min(pre1 - hr.model.min_err, hr.max_key)
                    key_left = min(biased_search_almost(self.geohash_index, 2, gh_new1,
                                                        pre1 + offset, l_bound1 + offset, r_bound1 + offset))
                if gh_new2:
                    pre2 = hr.model_predict(gh_new2)
                    l_bound2 = max(pre2 - hr.model.max_err, 0)
                    r_bound2 = min(pre2 - hr.model.min_err, hr.max_key)
                    key_right = max(biased_search_almost(self.geohash_index, 2, gh_new2,
                                                         pre2 + offset, l_bound2 + offset, r_bound2 + offset))
                # 5 filter all the point of scope[min_key/max_key] by range.contain(point)
                # 优化: region.contain->compare_func不同位置的点做不同的判断: 638->474mil
                result.extend([self.geohash_index[key][3] for key in range(key_left, key_right + 1)
                               if compare_func(self.geohash_index[key])])
        return result

    def knn_query_single(self, knn):
        """
        1. get the nearest key of query point
        2. get the nn points to create range query window
        3. filter point by distance
        主要耗时间：sbrin.knn_query.ranges_by_int/nn predict/精确过滤: 4.7mil/21mil/14.4mil
        """
        k = knn[2]
        # 1. get the nearest key of query point
        qp_g = self.meta.geohash.encode(knn[0], knn[1])
        qp_hr_key = self.point_query_hr(qp_g)
        qp_hr = self.history_ranges[qp_hr_key]
        # if hr is empty, TODO
        if qp_hr.number == 0:
            return []
        # if model, qp_key = point_query(geohash)
        else:
            offset = qp_hr_key * self.meta.threshold_number
            pre = qp_hr.model_predict(qp_g)
            l_bound = max(pre - qp_hr.model.max_err, 0)
            r_bound = min(pre - qp_hr.model.min_err, qp_hr.max_key)
            query_point_key = biased_search_almost(self.geohash_index, 2, qp_g,
                                                   pre + offset, l_bound + offset, r_bound + offset)[0]
        # 2. get the n points to create range query window
        # TODO: 两种策略，一种是左右找一半，但是如果跳跃了，window很大；
        #  还有一种是两边找n，减少跳跃，使window变小，当前是第二种
        tp_list = [[Point.distance_pow_point_list(knn, self.geohash_index[query_point_key]),
                    self.geohash_index[query_point_key][3]]]
        cur_key = query_point_key + 1
        cur_block_key = qp_hr_key
        i = 0
        while i < k - 1:
            if self.geohash_index[cur_key][2] == 0:
                cur_block_key += 1
                if cur_block_key > self.meta.last_hr:
                    break
                cur_key = cur_block_key * self.meta.threshold_number
            else:
                tp_list.append([Point.distance_pow_point_list(knn, self.geohash_index[cur_key]),
                                self.geohash_index[cur_key][3]])
                cur_key += 1
                i += 1
        cur_key = query_point_key - 1
        cur_block_key = qp_hr_key
        i = 0
        while i < k - 1:
            if self.geohash_index[cur_key][2] == 0:
                cur_block_key -= 1
                if cur_block_key < 0:
                    break
                cur_key = cur_block_key * self.meta.threshold_number + self.history_ranges[cur_block_key].max_key
            else:
                tp_list.append([Point.distance_pow_point_list(knn, self.geohash_index[cur_key]),
                                self.geohash_index[cur_key][3]])
                cur_key -= 1
                i += 1
        tp_list = sorted(tp_list)[:k]
        max_dist = tp_list[-1][0]
        if max_dist == 0:
            return [tp[1] for tp in tp_list]
        max_dist_pow = max_dist ** 0.5
        window = [knn[1] - max_dist_pow, knn[1] + max_dist_pow, knn[0] - max_dist_pow, knn[0] + max_dist_pow]
        gh1 = self.meta.geohash.encode(window[2], window[0])
        gh2 = self.meta.geohash.encode(window[3], window[1])
        tp_window_hrs = self.knn_query_hr(gh1, gh2, knn)
        position_func_list = [lambda reg: (None, None),  # window contain hr
                              lambda reg: (  # right
                                  None,
                                  self.meta.geohash.encode(window[3], reg.up)),
                              lambda reg: (  # left
                                  self.meta.geohash.encode(window[2], reg.bottom),
                                  None),
                              None,  # left-right
                              lambda reg: (  # up
                                  None,
                                  self.meta.geohash.encode(reg.right, window[1])),
                              lambda reg: (  # up-right
                                  None,
                                  gh2),
                              lambda reg: (  # up-left
                                  self.meta.geohash.encode(window[2], reg.bottom),
                                  self.meta.geohash.encode(reg.right, window[1])),
                              lambda reg: (None, None),  # up-left-right
                              lambda reg: (  # bottom
                                  self.meta.geohash.encode(reg.left, window[0]),
                                  None),
                              lambda reg: (  # bottom-right
                                  self.meta.geohash.encode(reg.left, window[0]),
                                  self.meta.geohash.encode(window[3], reg.up)),
                              lambda reg: (  # bottom-left
                                  gh1,
                                  None),
                              lambda reg: (  # bottom-left-right
                                  gh1,
                                  self.meta.geohash.encode(window[3], reg.up)),
                              None,
                              lambda reg: (  # bottom-up-right
                                  self.meta.geohash.encode(reg.left, window[0]),
                                  gh2),
                              lambda reg: (  # bottom-up-left
                                  gh1,
                                  self.meta.geohash.encode(reg.right, window[1])),
                              lambda reg: (  # bottom-up-left-right
                                  gh1,
                                  gh2)]
        tp_list = []
        for tp_window_hr in tp_window_hrs:
            if tp_window_hr[2] > max_dist:
                break
            hr = self.history_ranges[tp_window_hr[0]]
            if hr.number == 0:  # hr is empty
                continue
            offset = tp_window_hr[0] * self.meta.threshold_number
            key_left = offset
            key_right = key_left + hr.max_key
            gh_new1, gh_new2 = position_func_list[tp_window_hr[1]](hr.scope)
            if gh_new1:
                pre1 = hr.model_predict(gh_new1)
                l_bound1 = max(pre1 - hr.model.max_err, 0)
                r_bound1 = min(pre1 - hr.model.min_err, hr.max_key)
                key_left = min(biased_search_almost(self.geohash_index, 2, gh_new1,
                                                    pre1 + offset, l_bound1 + offset, r_bound1 + offset))
            if gh_new2:
                pre2 = hr.model_predict(gh_new2)
                l_bound2 = max(pre2 - hr.model.max_err, 0)
                r_bound2 = min(pre2 - hr.model.min_err, hr.max_key)
                key_right = max(biased_search_almost(self.geohash_index, 2, gh_new2,
                                                     pre2 + offset, l_bound2 + offset, r_bound2 + offset))
            # 3. filter point by distance
            tp_list.extend([[Point.distance_pow_point_list(knn, self.geohash_index[i]), self.geohash_index[i][3]]
                            for i in range(key_left, key_right + 1)])
            tp_list = sorted(tp_list)[:k]
            if tp_list == []:
                print(tp_list)
            max_dist = tp_list[-1][0]
        return [tp[1] for tp in tp_list]

    def save(self):
        sbrin_meta = np.array((self.meta.version, self.meta.threshold_number, self.meta.threshold_length,
                               self.meta.threshold_err, self.meta.threshold_summary, self.meta.threshold_merge,
                               self.meta.max_length, self.meta.geohash.data_precision,
                               self.meta.geohash.region.bottom, self.meta.geohash.region.up,
                               self.meta.geohash.region.left, self.meta.geohash.region.right, self.meta.last_hr),
                              dtype=[("0", 'i4'), ("1", 'i4'), ("2", 'i4'), ("3", 'i4'), ("4", 'i4'), ("5", 'i4'),
                                     ("6", 'i4'), ("7", 'i4'), ("8", 'f8'), ("9", 'f8'), ("10", 'f8'), ("11", 'f8'),
                                     ("12", 'i4')])
        sbrin_hr_models = np.array([hr.model for hr in self.history_ranges])
        sbrin_hrs = np.array([(hr.value, hr.length, hr.number, hr.value_diff,
                               hr.scope.bottom, hr.scope.up, hr.scope.left, hr.scope.right)
                              for hr in self.history_ranges],
                             dtype=[("0", 'i8'), ("1", 'i4'), ("2", 'i4'), ("3", 'i8'),
                                    ("4", 'f8'), ("5", 'f8'), ("6", 'f8'), ("7", 'f8')])
        sbrin_crs = []
        for cr in self.current_ranges:
            if cr.value is None:
                cr_list = [-1, -1, -1, -1, cr.number]
            else:
                cr_list = [cr.value.bottom, cr.value.up, cr.value.left, cr.value.right, cr.number]
            sbrin_crs.append(tuple(cr_list))
        sbrin_crs = np.array(sbrin_crs, dtype=[("0", 'f8'), ("1", 'f8'), ("2", 'f8'), ("3", 'f8'), ("4", 'i4')])
        np.save(os.path.join(self.model_path, 'sbrin_meta.npy'), sbrin_meta)
        np.save(os.path.join(self.model_path, 'sbrin_hrs.npy'), sbrin_hrs)
        np.save(os.path.join(self.model_path, 'sbrin_hr_models.npy'), sbrin_hr_models)
        np.save(os.path.join(self.model_path, 'sbrin_crs.npy'), sbrin_crs)
        geohash_index = np.array(self.geohash_index, dtype=[("0", 'f8'), ("1", 'f8'), ("2", 'i8'), ("3", 'i4')])
        np.save(os.path.join(self.model_path, 'geohash_index.npy'), geohash_index)

    def load(self):
        sbrin_meta = np.load(self.model_path + 'sbrin_meta.npy', allow_pickle=True).item()
        sbrin_hrs = np.load(self.model_path + 'sbrin_hrs.npy', allow_pickle=True)
        sbrin_hr_models = np.load(self.model_path + 'sbrin_hr_models.npy', allow_pickle=True)
        sbrin_crs = np.load(self.model_path + 'sbrin_crs.npy', allow_pickle=True)
        geohash_index = np.load(self.model_path + 'geohash_index.npy', allow_pickle=True)
        region = Region(sbrin_meta[8], sbrin_meta[9], sbrin_meta[10], sbrin_meta[11])
        geohash = Geohash.init_by_precision(data_precision=sbrin_meta[7], region=region)
        self.meta = Meta(sbrin_meta[0], sbrin_meta[1], sbrin_meta[2], sbrin_meta[3], sbrin_meta[4], sbrin_meta[5],
                         sbrin_meta[6], geohash, sbrin_meta[12])
        self.history_ranges = [HistoryRange(sbrin_hrs[i][0], sbrin_hrs[i][1], sbrin_hrs[i][2], sbrin_hr_models[i],
                                            Region(sbrin_hrs[i][4], sbrin_hrs[i][5], sbrin_hrs[i][6], sbrin_hrs[i][7]),
                                            sbrin_hrs[i][3]) for i in range(len(sbrin_hrs))]
        crs = []
        for i in range(len(sbrin_crs)):
            cr = sbrin_crs[i]
            if cr[0] == -1:
                region = None
            else:
                region = Region(cr[0], cr[1], cr[2], cr[3])
            crs.append(CurrentRange(region, cr[4]))
        self.current_ranges = crs
        self.geohash_index = geohash_index.tolist()

    def size(self):
        """
        size = sbrin_meta.npy + sbrin_hrs.npy + sbrin_hr_models.npy + sbrin_crs.npy + geohash_index.npy
        """
        # 实际上：
        # meta=os.path.getsize(os.path.join(self.model_path, "sbrin_meta.npy"))-128-64*2=4*9+8*4=68
        # hr=os.path.getsize(os.path.join(self.model_path, "sbrin_hrs.npy"))-128-64=hr_size*(4*2+8*6)=hr_size*56
        # model一致=os.path.getsize(os.path.join(self.model_path, "sbrin_hr_models.npy"))-128=hr_size*model_size
        # cr=os.path.getsize(os.path.join(self.model_path, "sbrin_crs.npy"))-128-64=cr_size*(4*1+8*4)=cr_size*36
        # geohash_index=os.path.getsize(os.path.join(self.model_path, "geohash_index.npy"))-128
        # =hr_size*meta.threashold_number*(8*3+4)
        # 理论上：
        # meta只存version/ts_number/ts_length/ts_err/ts_summary/ts_merge/max_length/first_cr=4*8=32
        # hr只存value/length/number=hr_size*(4*2+8*1)=hr_size*16
        # cr只存value/number=cr_size*(4*1+8*4)=cr_size*36
        # geohash_index为data_len*(8*3+4)=data_len*28
        data_len = len([geohash for geohash in self.geohash_index if geohash[2] != 0])
        hr_size = len(self.history_ranges)
        cr_size = len(self.current_ranges)
        return 32 + \
               hr_size * 16 + \
               os.path.getsize(os.path.join(self.model_path, "sbrin_hr_models.npy")) - 128 + \
               cr_size * 36 + \
               data_len * 28


class Meta:
    def __init__(self, version, threshold_number, threshold_length, threshold_err, threshold_summary,
                 threshold_merge, max_length, geohash, last_hr):
        # BRIN
        self.version = version
        # SBRIN
        # self.first_cr = first_cr
        self.threshold_number = threshold_number
        self.threshold_length = threshold_length
        self.threshold_err = threshold_err
        self.threshold_summary = threshold_summary
        self.threshold_merge = threshold_merge
        self.max_length = max_length
        # For compute
        self.geohash = geohash
        self.last_hr = last_hr


class HistoryRange:
    def __init__(self, value, length, number, model, scope, value_diff):
        # BRIN
        self.value = value
        # SBRIN
        self.length = length
        self.number = number
        self.model = model
        # For compute
        self.scope = scope
        self.value_diff = value_diff
        self.max_key = number - 1

    def model_predict(self, x):
        x = self.model.predict((x - self.value) / self.value_diff - 0.5)
        if x <= 0:
            return 0
        elif x >= 1:
            return self.max_key
        return int(self.max_key * x)

    def model_update(self, xs):
        err = self.model.predicts((np.array(xs) - self.value) / self.value_diff - 0.5) * self.max_key \
              - np.arange(len(xs))
        self.model.min_err = math.ceil(err.min())
        self.model.max_err = math.ceil(err.max())


class CurrentRange:
    def __init__(self, value, number):
        # BRIN
        self.value = value
        # SBRIN
        self.number = number
        # For compute


class AbstractNN:
    def __init__(self, weights, hl_nums, min_err, max_err):
        self.weights = weights
        self.hl_nums = hl_nums
        self.min_err = min_err
        self.max_err = max_err

    # model.predict有小偏差，可能是exp的e和elu的e不一致
    def predict(self, x):
        for i in range(self.hl_nums):
            x = sigmoid(x * self.weights[i * 2] + self.weights[i * 2 + 1])
        return (x * self.weights[-2] + self.weights[-1])[0, 0]

    def predicts(self, xs):
        for i in range(self.hl_nums):
            xs = sigmoid(xs * self.weights[i * 2] + self.weights[i * 2 + 1])
        return (xs * self.weights[-2] + self.weights[-1]).T.A


# @profile(precision=8)
def main():
    os.chdir(os.path.dirname(os.path.realpath(__file__)))
    data_path = '../../data/index/trip_data_1_filter_10w_sorted.npy'
    model_path = "model/sbrin_10w/"
    if os.path.exists(model_path) is False:
        os.makedirs(model_path)
    index = SBRIN(model_path=model_path)
    index_name = index.name
    load_index_from_json = True
    if load_index_from_json:
        index.load()
    else:
        index.logging.info("*************start %s************" % index_name)
        start_time = time.time()
        # data_list = np.load(data_path, allow_pickle=True)[:, [10, 11, -1]]
        data_list = np.load(data_path, allow_pickle=True)
        # 按照pagesize=4096, prefetch=256, size(pointer)=4, size(x/y/g)=8, sbrin整体连续存, meta一个page, br分页存，model(2009大小)单独存
        # hr体积=value/length/number=4*2+8*1=16，一个page存256个hr
        # cr体积=value/number=4*1+8*4=36，一个page存113个cr
        # model体积=2009，一个page存2个model
        # data体积=x/y/g/key=8*3+4=28，一个page存146个data
        # 10w数据，[1000]参数下：大约有289个cr
        # 1meta page，289/256=2hr page，0cr page, 289/2=145model page，10w/146=685data page
        # 单次扫描IO=读取sbrin+读取对应model+读取model对应geohash数据=1+1+误差范围/146/512
        # 索引体积=geohash索引+meta+hrs+model+crs
        index.build(data_list=data_list,
                    threshold_number=1000,
                    data_precision=6,
                    region=Region(40, 42, -75, -73),
                    threshold_err=0,
                    threshold_summary=1000,
                    threshold_merge=5,
                    use_threshold=False,
                    threshold=20,
                    core=[1, 128, 1],
                    train_step=5000,
                    batch_num=64,
                    learning_rate=0.1,
                    retrain_time_limit=2,
                    thread_pool_size=6,
                    save_nn=True,
                    weight=1)
        index.save()
        end_time = time.time()
        build_time = end_time - start_time
        index.logging.info("Build time: %s" % build_time)
    logging.info("Index size: %s" % index.size())
    path = '../../data/query/point_query_10w.npy'
    point_query_list = np.load(path, allow_pickle=True).tolist()
    start_time = time.time()
    results = index.point_query(point_query_list)
    end_time = time.time()
    search_time = (end_time - start_time) / len(point_query_list)
    logging.info("Point query time: %s" % search_time)
    np.savetxt(model_path + 'point_query_result.csv', np.array(results, dtype=object), delimiter=',', fmt='%s')
    path = '../../data/query/range_query_10w.npy'
    range_query_list = np.load(path, allow_pickle=True).tolist()
    start_time = time.time()
    results = index.range_query(range_query_list)
    end_time = time.time()
    search_time = (end_time - start_time) / len(range_query_list)
    logging.info("Range query time:  %s" % search_time)
    np.savetxt(model_path + 'range_query_result.csv', np.array(results, dtype=object), delimiter=',', fmt='%s')
    path = '../../data/query/knn_query_10w.npy'
    knn_query_list = np.load(path, allow_pickle=True).tolist()
    start_time = time.time()
    results = index.knn_query(knn_query_list)
    end_time = time.time()
    search_time = (end_time - start_time) / len(knn_query_list)
    logging.info("KNN query time:  %s" % search_time)
    np.savetxt(model_path + 'knn_query_result.csv', np.array(results, dtype=object), delimiter=',', fmt='%s')
    path = '../../data/table/trip_data_2_filter_10w.npy'
    insert_data_list = np.load(path, allow_pickle=True)[:, [10, 11, -1]]
    # profile = line_profiler.LineProfiler(index.update_hr)
    # profile.enable()
    index.insert(insert_data_list.tolist())
    # profile.disable()
    # profile.print_stats()
    start_time = time.time()
    end_time = time.time()
    logging.info("Insert time: %s" % (end_time - start_time))


if __name__ == '__main__':
    main()
