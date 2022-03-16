import os
import time

import pandas as pd
from memory_profiler import profile

from src.spatial_index.common_utils import Region
from src.spatial_index.geohash_model_index import GeoHashModelIndex


@profile(precision=8)
def load_model_size(model_path):
    index = GeoHashModelIndex(model_path=model_path)
    index.load()
    index = None


"""
1. 读取数据
2. 设置实验参数
3. 开始实验
3.1 快速构建精度低的
3.2 构建精度高的
"""
if __name__ == '__main__':
    # 1. 读取数据
    os.chdir(os.path.dirname(os.path.realpath(__file__)))
    path = '../../data/trip_data_1_100000.csv'
    # path = '../../data/trip_data_1_filter.csv'
    train_set_xy = pd.read_csv(path)
    # 2. 设置实验参数
    n_list = [2500, 5000, 10000, 20000, 40000]
    # 3. 开始实验
    # 3.1 快速构建精度低的
    for n in n_list:
        model_path = "model/gm_index/n_" + str(n) + "/"
        index = GeoHashModelIndex(model_path=model_path)
        index_name = index.name
        print("*************start %s************" % model_path)
        start_time = time.time()
        index.build(data=train_set_xy, max_num=n, data_precision=6, region=Region(40, 42, -75, -73),
                    use_threshold=False,
                    threshold=20,
                    core=[1, 128, 1],
                    train_step=500,
                    batch_size=1024,
                    learning_rate=0.01,
                    retrain_time_limit=20,
                    thread_pool_size=1)
        end_time = time.time()
        build_time = end_time - start_time
        print("Build time: %s " % build_time)
        index.save()
        model_num = len(index.gm_dict)
        print("Model num: %s " % len(index.gm_dict))
        model_precisions = [(nn.max_err - nn.min_err) for nn in index.gm_dict if nn is not None]
        model_precisions_avg = sum(model_precisions) / model_num
        print("Model precision avg: %s" % model_precisions_avg)
        path = '../../data/trip_data_1_point_query.csv'
        point_query_df = pd.read_csv(path, usecols=[1, 2, 3])
        point_query_list = point_query_df.drop("count", axis=1).values.tolist()
        start_time = time.time()
        results = index.point_query(point_query_list)
        end_time = time.time()
        search_time = (end_time - start_time) / len(point_query_list)
        print("Point query time ", search_time)
    # 3.2 构建精度高的
    for n in n_list:
        model_path = "model/gm_index/n_" + str(n) + "_precision/"
        index = GeoHashModelIndex(model_path=model_path)
        index_name = index.name
        print("*************start %s************" % model_path)
        start_time = time.time()
        index.build(data=train_set_xy, max_num=n, data_precision=6, region=Region(40, 42, -75, -73),
                    use_threshold=True,
                    threshold=20,
                    core=[1, 128, 1],
                    train_step=500,
                    batch_size=1024,
                    learning_rate=0.01,
                    retrain_time_limit=20,
                    thread_pool_size=6)
        end_time = time.time()
        build_time = end_time - start_time
        print("Build time: %s " % build_time)
        index.save()
        model_num = len(index.gm_dict)
        print("Model num: %s " % len(index.gm_dict))
        model_precisions = [(nn.max_err - nn.min_err) for nn in index.gm_dict if nn is not None]
        model_precisions_avg = sum(model_precisions) / model_num
        print("Model precision avg: %s" % model_precisions_avg)
        path = '../../data/trip_data_1_point_query.csv'
        point_query_df = pd.read_csv(path, usecols=[1, 2, 3])
        point_query_list = point_query_df.drop("count", axis=1).values.tolist()
        start_time = time.time()
        results = index.point_query(point_query_list)
        end_time = time.time()
        search_time = (end_time - start_time) / len(point_query_list)
        print("Point query time ", search_time)

