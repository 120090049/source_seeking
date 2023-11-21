import sys
# sys.path.append('./rt_erg_lib/')
from double_integrator import DoubleIntegrator
from ergodic_control import RTErgodicControl
from target_dist import TargetDist
from scipy.spatial.distance import cdist
from scipy.spatial import Voronoi, distance
import matplotlib.pyplot as plt

from utils import convert_phi2phik, convert_ck2dist, convert_traj2ck, convert_phik2phi, find_peak
import numpy as np
from scipy.io import loadmat

import rospy
from grid_map_msgs.msg import GridMap
from source_seeking.msg import Ck
from environment_and_measurement import f, sampling, source, source_value # sampling function is just f with noise

from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import RBF, WhiteKernel


def kernel_initial(
            σf_initial=1.0,         # covariance amplitude
            ell_initial=1.0,        # length scale
            σn_initial=0.1          # noise level
        ):
            return σf_initial**2 * RBF(length_scale=ell_initial, length_scale_bounds=(0.5, 2)) + WhiteKernel(noise_level=σn_initial)

class Controller(object): # python (x,y) therefore col index first, row next
    # robot state + controller !
    def __init__(self, start_position, index, test_resolution = [50,50], field_size = [10,10]):
        
        ## robot index
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
        # test_resolution
        self.test_resolution = test_resolution # [50, 50]
        # real size
        self.field_size = field_size # [10, 10]
        
        # for ES
        grid_2_r_w = np.meshgrid(np.linspace(0, 1, int(test_resolution[0])), np.linspace(0, 1, int(test_resolution[1])))
        self.grid = np.c_[grid_2_r_w[0].ravel(), grid_2_r_w[1].ravel()] # (2500,2)

        # for gp
        X_test_x = np.linspace(0, field_size[0], test_resolution[0])
        X_test_y = np.linspace(0, field_size[1], test_resolution[1])
        X_test_xx, X_test_yy = np.meshgrid(X_test_x, X_test_y)
        self.X_test = np.vstack(np.dstack((X_test_xx, X_test_yy))) # (2500,2)
        
        ## ROBOT information
        # robot dynamics
        self.robot_dynamic = DoubleIntegrator() # robot controller system
        self.robot_state     = np.zeros(self.robot_dynamic.observation_space.shape[0])
        # robot initial location
        self.robot_state[:2] = np.array([start_position[0]/self.field_size[0], start_position[1]/self.field_size[1]])
        self.robot_dynamic.reset(self.robot_state)
        self.Erg_ctrl    = RTErgodicControl(DoubleIntegrator(), weights=0.01, horizon=15, num_basis=10, batch_size=-1)
        self.Mpc_ctrl    = 
        # samples 
        self.samples_X = np.array([start_position])
        self.samples_Y = np.array(sampling([start_position]))
        
        self.trajectory = [start_position]
        
        ## ROBOT neigibours
        self.neighbour = None  
        self.responsible_region = np.zeros(self.test_resolution)
        
        print("Controller Succcessfully Initialized! Initial position is: ", start_position)
        self.sent_samples = []
        
        ## GP
        self.gp = GaussianProcessRegressor(
            kernel=kernel_initial(),
            n_restarts_optimizer=10
        )
        self.estimation = None
        self.variance = None
        self.peaks_cord = None
        self.peak_LCB = None
        ## 
    
    def receive_prior_knowledge(self, X_train=None, y_train=None):
        if X_train is not None:
            assert X_train.shape[0] == y_train.shape[0], "Error: The number of elements in X_train and y_train must be equal."
            self.samples_X = np.concatenate((self.samples_X, X_train), axis=0)
            self.samples_Y = np.concatenate((self.samples_Y, y_train), axis=0)
            
    # step 1 (update neighbour postions + know responsible region + exchange samples)
    def voronoi_update(self, neighour_robot_index, robots_locations): # set responsible region and neighbours
        # 1) update neighbour postions
        self.neighbour = neighour_robot_index
        self.neighbour_loc = [robots_locations[i] for i in self.neighbour]
        # 2) know responsible region
        # 采样精度是50*50，机器人实际的坐标是10*10
        
        scale_row = self.test_resolution[0]/self.field_size[0]
        scale_col = self.test_resolution[1]/self.field_size[1]
        
        robots_locations = np.array(robots_locations)
        robots_locations_scaled = np.zeros_like(robots_locations)  # 创建一个与 robots 形状相同的全零数组
        robots_locations_scaled[:, 0] = robots_locations[:, 0] * scale_col  # 将第一列（X坐标）乘以10
        robots_locations_scaled[:, 1] = robots_locations[:, 1] * scale_row  # 将第二列（Y坐标）乘以20
        
        grid_points = np.array([(x, y) for x in range(self.test_resolution[0]) for y in range(self.test_resolution[1])])

        # 计算每个格点到所有机器人的距离
        distances = cdist(grid_points, robots_locations_scaled, metric='euclidean')

        # 找到每个格点最近的机器人
        closest_robot = np.argmin(distances, axis=1)

        # 标记距离 [0,0] 机器人最近的格点为 1
        self.responsible_region = np.zeros(self.test_resolution)
        
        for i, robot_index in enumerate(closest_robot):
            if robot_index == self.index:
                y, x = grid_points[i]
                self.responsible_region[x, y] = 1

        # 3) exchange samples
        exchange_dictionary_X = {}
        exchange_dictionary_y = {}
        
        closest_point_index_list = []
        
        sample_index = 0
        for sample in self.samples_X:
            if sample.tolist() not in self.sent_samples:
                distances = [distance.euclidean(sample, robo) for robo in robots_locations]
                # 找到距离最近的生成点的索引
                closest_point_index = np.argmin(distances)
                closest_point_index_list.append(closest_point_index)
                
                if closest_point_index != self.index:
                    if closest_point_index not in exchange_dictionary_X:
                        exchange_dictionary_X[closest_point_index] = []
                        exchange_dictionary_y[closest_point_index] = []  # 如果不存在，则初始化一个空列表
                    exchange_dictionary_X[closest_point_index].append(sample)
                    exchange_dictionary_y[closest_point_index].append(self.samples_Y[sample_index])
                    
                    self.sent_samples.append(sample.tolist())
            sample_index += 1

        return exchange_dictionary_X, exchange_dictionary_y
    
    def receive_samples(self, exchanged_samples_X, exchanged_samples_y):
        if exchanged_samples_X is not None:
            self.samples_X = np.concatenate((self.samples_X, exchanged_samples_X), axis=0)
            self.samples_Y = np.concatenate((self.samples_Y, exchanged_samples_y), axis=0)
    
    # step 2 calcualte GP
    def gp_regresssion(self, ucb_coeff=2): # the X_train, y_train is only for the prior knowledge
        # # 找到 samples_X 中重复元素的索引
        # 根据这些索引保留 samples_X 和 samples_Y 中的非重复元素
        _, unique_indices = np.unique(self.samples_X, axis=0, return_index=True)
        self.samples_X = self.samples_X[unique_indices]
        self.samples_Y = self.samples_Y[unique_indices]      
        self.gp.fit(self.samples_X, self.samples_Y)
        μ_test, σ_test = self.gp.predict(self.X_test, return_std=True)
        
        
        
        ucb = μ_test + ucb_coeff*σ_test
        self.estimation = μ_test.reshape(self.test_resolution)
        self.variance = σ_test.reshape(self.test_resolution)

        
        ucb = ucb.reshape(self.test_resolution)
        
        # calculate phik based on ucb
        self.phik = convert_phi2phik(self.Erg_ctrl.basis, ucb, self.grid)
        phi = convert_phik2phi(self.Erg_ctrl.basis, self.phik , self.grid)

        # plt.imshow(phi.reshape(50,50), cmap='viridis')
        # cbar = plt.colorbar()
        # cbar.set_label('Value')
        # plt.show()
        # self.Erg_ctrl.phik = convert_phi2phik(self.Erg_ctrl.basis, phi_vals, self.grid)
        # return μ_test, σ_test
        self.estimate_source(lcb_coeff=2)
        return self.estimation*self.responsible_region, ucb
    
    # step 3 exchange phik ck 
    def send_out_phik(self):
        if self.phik is not None:
            return self.phik
    
    def receive_phik_consensus(self, phik_pack):
        phik = [self.phik]
        if self.phik is not None:
            for neighbour_index in self.neighbour:
                phik.append(phik_pack[neighbour_index])
            phik_consensus = np.mean(phik, axis=0)
            self.Erg_ctrl.phik = 0.0025*phik_consensus
        phi = convert_phik2phi(self.Erg_ctrl.basis, phik_consensus , self.grid)

        return phi.reshape(self.test_resolution)
        
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
        
    # step 4 move will ergodic control
    def get_nextpts(self, phi_vals=None, control_mode="ES_NORMAL"):

        sample_steps = 1
        setpoints = []
        
        active_sensing = True
        target = None
        if control_mode!="ES_UNIFORM" and  self.peak_LCB and np.max(self.peak_LCB) > 0.1:      
            index = np.argmax(self.peak_LCB)
            distance = cdist([self.trajectory[-1]], [self.peaks_cord[index]])[0][0]
            target = self.peaks_cord[index]
            print("For robot ", self.index, "its target is ", self.peaks_cord[index], "distance = ", distance)
            active_sensing = False
        # for target_index in range(len(self.peak_LCB)):
        #     if self.peak_LCB[target_index] > 0.15:
        
        if active_sensing:
            if "ES" in control_mode:
                if phi_vals is not None:
                    phi_vals /= np.sum(phi_vals)
                    self.Erg_ctrl.phik = convert_phi2phik(self.Erg_ctrl.basis, phi_vals, self.grid)
                
                if control_mode == "ES_NORMAL":
                    print(self.peaks_cord, self.peak_LCB)

                if control_mode == "ES_UNIFORM":
                    # setting the phik on the ergodic controller    
                    phi_vals = np.ones(self.test_resolution)
                    phi_vals /= np.sum(phi_vals)
                    self.Erg_ctrl.phik = convert_phi2phik(self.Erg_ctrl.basis, phi_vals, self.grid)

                i = 0
                while (i < sample_steps):
                    i += 1
                    ctrl = self.Erg_ctrl(self.robot_state)
                    self.robot_state = self.robot_dynamic.step(ctrl)         
                    setpoints.append([ self.field_size[0]*self.robot_state[0], self.field_size[1]*self.robot_state[1] ])
            
            elif control_mode == "DUCB":
                # 计算每个格点到所有机器人的距离
                own_loc = self.trajectory[-1]
                distances = cdist(self.X_test, [own_loc], metric='euclidean').reshape(self.test_resolution)
                kapa = 20
                ucb = self.estimation + kapa * self.variance
                gama = -0.3
                ducb = ucb + gama * distances

                temp = (ducb+100)  * self.responsible_region
                max_index_1d = np.argmax(temp)
                # max_index_2d = np.unravel_index(max_index_1d, self.test_resolution)
                target_pt = self.X_test[max_index_1d]
                setpoints.append(target_pt)
            
            elif control_mode == "Boundry_setting":
                own_loc = self.trajectory[-1]
                distances = cdist(self.X_test, [own_loc], metric='euclidean').reshape(self.test_resolution)
                distances[distances > 0.5] = 1
                distances[distances <= 0.5] = 0
                ucb = (self.estimation + 10 * self.variance) * self.responsible_region 
                ucb_trun = ucb * (1-distances)
                
                max_index_1d = np.argmax(ucb_trun)
                # max_index_2d = np.unravel_index(max_index_1d, self.test_resolution)
                target_pt = self.X_test[max_index_1d]
                setpoints.append(target_pt)
                
                # # 创建一个图和两个子图的轴
                # fig, (ax1, ax2) = plt.subplots(1, 2)

                # # 在第一个子图上显示图像
                # im1 = ax1.imshow(ucb, cmap='viridis', origin='lower')
                # ax1.scatter(5*np.array(setpoints)[:, 0], 5*np.array(setpoints)[:, 1],s=5, c='red', zorder=1)
                # ax1.set_xlabel('X Label')  # 设置第一个子图的X轴标签
                # ax1.set_title('UCB')  # 设置第一个子图的标题
                # cbar1 = fig.colorbar(im1, ax=ax1)  # 在第一个子图上添加颜色条
                # cbar1.set_label('Value')  # 设置第一个子图的颜色条标签

                # # 在第二个子图上显示图像
                # im2 = ax2.imshow(ucb_trun, cmap='viridis', origin='lower')
                # ax2.set_xlabel('X Label')  # 设置第二个子图的X轴标签
                # ax2.set_title('ucb_trun')  # 设置第二个子图的标题
                # cbar2 = fig.colorbar(im2, ax=ax2)  # 在第二个子图上添加颜色条
                # cbar2.set_label('Value')  # 设置第二个子图的颜色条标签
                # plt.show()
                
            elif control_mode == "UCB_dynamic_model":
                pass
        
        # source seeking
        else:
            
               
        self.trajectory = self.trajectory + setpoints
        setpoints = np.array(setpoints)
        self.samples_X = np.concatenate((self.samples_X, setpoints), axis=0)
        self.samples_Y = np.concatenate((self.samples_Y, sampling(setpoints)), axis=0)
        
        return setpoints
    
    
    # Tools
    def get_trajectory(self):
        # return [self.field_size[0]*self.robot_state[0], self.field_size[1]*self.robot_state[1]]
        return self.trajectory
    
    def estimate_source(self, lcb_coeff=2):
        peaks = np.array(find_peak(self.estimation)) # [col_mat, ...]

        real_res_ratio = self.field_size[0]/self.test_resolution[0] # 10m/50 = 0.2m
        increased_resolution_ratio = self.test_resolution[0]/2 # 25
        real_res_ratio_new = real_res_ratio/increased_resolution_ratio
        X_test = self.X_test/increased_resolution_ratio # increase resolution of sampling point [[0,0.2]] => [[0,0.2/ratio]]
        # new_ratio = [ratio[0]*(2/self.test_resolution[0]), ratio[1]*(2/self.test_resolution[1])]
        peaks_cord = []
        for peak in peaks:
            x, y =  real_res_ratio * (peak[0]-1),  real_res_ratio * (peak[1]-1) # real coordinate (start from upper left)
            X_test_copy = np.copy(X_test)
            X_test_copy[:, 0] += x
            X_test_copy[:, 1] += y  
            μ_test, σ_test = self.gp.predict(X_test_copy, return_std=True)
            new_peaks = np.array(find_peak(μ_test.reshape(self.test_resolution), strict=False)[0])
            
            peak_x, peak_y = x + real_res_ratio_new*new_peaks[0], y + real_res_ratio_new*new_peaks[0]
            peaks_cord.append([peak_x, peak_y])

        self.peaks_cord = []
        self.peak_LCB = []
        if len(peaks_cord):
            μ, σ = self.gp.predict(peaks_cord, return_std=True)
            own_loc = self.trajectory[-1]
            neighbour_loc = self.neighbour_loc
            bots_loc = [own_loc] + neighbour_loc
            distances = cdist(np.array(peaks_cord), bots_loc, metric='euclidean')
            closest_robot_index = np.argmin(distances, axis=1)
            
            i=0
            for peak in peaks_cord:
                if closest_robot_index[i] == 0: 
                    LCB = μ[i] - lcb_coeff*σ[i]
                    self.peak_LCB.append(LCB)
                    self.peaks_cord.append(peak)
                i+=1

            
        return self.peaks_cord, self.peak_LCB
    
    def get_estimated_source(self):
        return self.peaks_cord, self.peak_LCB 
    
    # def _ck_link_callback(self, msg):
    #     received_robo_index = int(msg.name)
    #     if received_robo_index != self.index:
    #         if received_robo_index in self.neighbour: # is neighbour (add or update)
    #             # if received_robo_index in self._ck_dict: # update
    #             # else if received_robo_index not in self._ck_dict: # not in dict 
    #             self._ck_dict.update({received_robo_index : np.array(msg.ck)})
    #         elif received_robo_index in self._ck_dict: # not neighour any more 
    #             del self._ck_dict[received_robo_index]
    
    # def run(self):
    #     # rospy.init_node(self.agent_name)
    #     rate = rospy.Rate(10)
    #     while not rospy.is_shutdown():
    #         # self.step()
    #         rate.sleep() 
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