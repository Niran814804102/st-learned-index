import os
import sys
import time

import numpy as np

sys.setrecursionlimit(5000)
sys.path.append('/home/zju/wlj/st-learned-index')
from src.spatial_index.common_utils import Region, quick_sort
from src.spatial_index.geohash_utils import Geohash

os.chdir(os.path.dirname(os.path.realpath(__file__)))
# path = '../../data/table/trip_data_1_filter_10w.npy'
path = '../../data/table/trip_data_1_filter.npy'
data = np.load(path, allow_pickle=True)[:, 10:12]
data_len = len(data)
geohash = Geohash.init_by_precision(data_precision=6, region=Region(40, 42, -75, -73))
start_time = time.time()
data = [(data[i][0], data[i][1], geohash.encode(data[i][0], data[i][1]), i) for i in range(data_len)]
quick_sort(data, 2, 0, data_len - 1)
data = np.array(data, dtype=[("0", 'f8'), ("1", 'f8'), ("2", 'i8'), ("3", 'i4')])
end_time = time.time()
search_time = end_time - start_time
print("Load data time: %s" % search_time)
