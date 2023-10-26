import sys
# sys.path.append('./rt_erg_lib/')
from double_integrator import DoubleIntegrator
from ergodic_control import RTErgodicControl
from target_dist import TargetDist
from scipy.spatial.distance import cdist
import matplotlib.pyplot as plt

from utils import convert_phi2phik, convert_ck2dist, convert_traj2ck, convert_phik2phi
import numpy as np
from scipy.io import loadmat

import rospy
from grid_map_msgs.msg import GridMap
from source_seeking.msg import Ck


class Controller(object): # python (x,y) therefore col index first, row next
    # robot state + controller !
    def __init__(self, start_position, index, resolution = [50,50], field_size = [10,10]):
    # robot index
        self.index = index
        self.agent_name = str(index)
        
    ## robot node and communication

        # # send out phik
        # self._phik_msg    = Ck()
        # self._phik_msg.name = self.agent_name
        # self._phik_pub = rospy.Publisher('phik_link', Ck, queue_size=1)
        # # receive phik
        # rospy.Subscriber('phik_link', Ck, self._phik_link_callback)
        
        # # send out ck
        # self._ck_msg    = Ck()
        # self._ck_msg.name = self.agent_name
        # self._ck_pub = rospy.Publisher('ck_link', Ck, queue_size=1)
        # # send out ck
        # rospy.Subscriber('ck_link', Ck, self._ck_link_callback)
        # self._ck_dict   = {}
    
    ## field information
        # resolution
        self.row = resolution[0] # 50
        self.col = resolution[1]
        # real size
        self.field_row = field_size[0] # 10
        self.field_col = field_size[1]
        grid_2_r_w = np.meshgrid(np.linspace(0, 1, int(self.row)), np.linspace(0, 1, int(self.col)))
        self.grid = np.c_[grid_2_r_w[0].ravel(), grid_2_r_w[1].ravel()]
        
    # ROBOT information
        # robot dynamics
        self.robot_dynamic = DoubleIntegrator() # robot controller system
        self.robot_state     = np.zeros(self.robot_dynamic.observation_space.shape[0])
        # robot initial location
        self.robot_state[:2] = np.array([start_position[0]/self.field_row, start_position[1]/self.field_col])
        self.robot_dynamic.reset(self.robot_state)
        
        # ergodic controller
        self.Erg_ctrl    = RTErgodicControl(DoubleIntegrator(), weights=0.01, horizon=15, num_basis=10, batch_size=-1)
        self.pre_state = None
    
    # ROBOT neigibours
        self.neighbour = None  
        self.responsible_region = np.zeros((self.row, self.col))
        print("Controller Succcessfully Initialized! Initial position is: ", start_position)
    
    def get_location(self):
        return [self.field_row*self.robot_state[0], self.field_col*self.robot_state[1]]
    
    # def _ck_link_callback(self, msg):
    #     received_robo_index = int(msg.name)
    #     if received_robo_index != self.index:
    #         if received_robo_index in self.neighbour: # is neighbour (add or update)
    #             # if received_robo_index in self._ck_dict: # update
    #             # else if received_robo_index not in self._ck_dict: # not in dict 
    #             self._ck_dict.update({received_robo_index : np.array(msg.ck)})
    #         elif received_robo_index in self._ck_dict: # not neighour any more 
    #             del self._ck_dict[received_robo_index]
    def send_out_ck(self):
        ck = self.Erg_ctrl.get_ck()
        if ck is not None:
            return ck 
    
    def receive_ck_consensus(self, ck_pack):
        my_ck = self.Erg_ctrl.get_ck()
        if my_ck is not None:
            cks = [my_ck]
            for neighbour_index in self.neighbour:
                cks.append(ck_pack[neighbour_index])
            ck_mean = np.mean(cks, axis=0)
            self.Erg_ctrl.receieve_consensus_ck(ck_mean)
        
    
    # def _phik_link_callback(self, msg):
    #     return
    
    # def ck_concensus(self):
    #     my_ck = self.Erg_ctrl.get_ck()
    #     if len(self._ck_dict.keys()) >= 1:
    #         print("consensus!!!")
    #         cks = [my_ck]
    #         for key in self._ck_dict.keys():
    #             cks.append(self._ck_dict[key])
    #         ck_mean = np.mean(cks, axis=0)
    #         self.Erg_ctrl.set_ck(ck_mean)
    #         self._ck_msg.ck = ck_mean.copy()
            
    def voronoi_update(self, neighour_robot_index, robots_locations): # set responsible region and neighbours
        
        self.neighbour = neighour_robot_index
        
        scale_row = self.row/self.field_row
        scale_col = self.col/self.field_col
        
        robots_locations = np.array(robots_locations)
        robots_locations_scaled = np.zeros_like(robots_locations)  # 创建一个与 robots 形状相同的全零数组
        robots_locations_scaled[:, 0] = robots_locations[:, 0] * scale_col  # 将第一列（X坐标）乘以10
        robots_locations_scaled[:, 1] = robots_locations[:, 1] * scale_row  # 将第二列（Y坐标）乘以20
        
        grid_points = np.array([(x, y) for x in range(self.row) for y in range(self.col)])

        # 计算每个格点到所有机器人的距离
        distances = cdist(grid_points, robots_locations_scaled, metric='euclidean')

        # 找到每个格点最近的机器人
        closest_robot = np.argmin(distances, axis=1)

        # 标记距离 [0,0] 机器人最近的格点为 1
        for i, robot_index in enumerate(closest_robot):
            if robot_index == self.index:
                y, x = grid_points[i]
                self.responsible_region[x, y] = 1
    
    def get_nextpts(self, phi_vals): 
        sample_steps = 1
        setpoints = []
        
        # self.ck_concensus()
        # self._ck_pub.publish(self._ck_msg)
        
        # setting the phik on the ergodic controller
        phi_vals = np.array(phi_vals)
        phi_vals /= np.sum(phi_vals)

        self.Erg_ctrl.phik = convert_phi2phik(self.Erg_ctrl.basis, phi_vals, self.grid)
        
        i = 0
        while (i < sample_steps):
            i += 1
            ctrl = self.Erg_ctrl(self.robot_state)
            self.robot_state = self.robot_dynamic.step(ctrl)
                  
            setpoints.append([ self.field_row*self.robot_state[0], self.field_col*self.robot_state[1] ])
 
        
        setpoints = np.array(setpoints)
        return setpoints
    
    # def run(self):
    #     # rospy.init_node(self.agent_name)
    #     rate = rospy.Rate(10)
    #     while not rospy.is_shutdown():
    #         # self.step()
    #         rate.sleep() 
   