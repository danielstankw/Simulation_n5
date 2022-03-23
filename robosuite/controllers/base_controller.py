import abc
from collections.abc import Iterable
import numpy as np
import mujoco_py
import robosuite.utils.macros as macros
import robosuite.utils.transform_utils as T
from copy import deepcopy

class Controller(object, metaclass=abc.ABCMeta):
    """
    General controller interface.

    Requires reference to mujoco sim object, eef_name of specific robot, relevant joint_indexes to that robot, and
    whether an initial_joint is used for nullspace torques or not

    Args:
        sim (MjSim): Simulator instance this controller will pull robot state updates from

        eef_name (str): Name of controlled robot arm's end effector (from robot XML)

        joint_indexes (dict): Each key contains sim reference indexes to relevant robot joint information, namely:

            :`'joints'`: list of indexes to relevant robot joints
            :`'qpos'`: list of indexes to relevant robot joint positions
            :`'qvel'`: list of indexes to relevant robot joint velocities

        actuator_range (2-tuple of array of float): 2-Tuple (low, high) representing the robot joint actuator range
    """

    def __init__(self,
                 sim,
                 eef_name,
                 joint_indexes,
                 actuator_range,
                 ):

        # Actuator range
        self.actuator_min = actuator_range[0]
        self.actuator_max = actuator_range[1]

        # Attributes for scaling / clipping inputs to outputs
        self.action_scale = None
        self.action_input_transform = None
        self.action_output_transform = None

        # Private property attributes
        self.control_dim = None
        self.output_min = None
        self.output_max = None
        self.input_min = None
        self.input_max = None

        # mujoco simulator state
        self.sim = sim
        self.model_timestep = macros.SIMULATION_TIMESTEP
        self.eef_name = eef_name
        self.joint_index = joint_indexes["joints"]
        self.qpos_index = joint_indexes["qpos"]
        self.qvel_index = joint_indexes["qvel"]

        # robot states
        self.ee_pos = None
        self.ee_ori_mat = None
        self.ee_pos_vel = None
        self.ee_ori_vel = None
        self.joint_pos = None
        self.joint_vel = None

        # dynamics and kinematics
        self.J_pos = None
        self.J_ori = None
        self.J_full = None
        self.mass_matrix = None

        # Joint dimension
        self.joint_dim = len(joint_indexes["joints"])

        # Torques being outputted by the controller
        self.torques = None

        # Update flag to prevent redundant update calls
        self.new_update = True

        # Move forward one timestep to propagate updates before taking first update
        self.sim.forward()

        # Initialize controller by updating internal state and setting the initial joint, pos, and ori
        self.update()
        self.initial_joint = self.joint_pos
        self.initial_ee_pos = self.ee_pos
        self.initial_ee_ori_mat = self.ee_ori_mat
        self.initial_ee_ori_vel = self.ee_ori_vel

        self.desired_vec_fin = []
        self.switch = 0

    @abc.abstractmethod
    def run_controller(self):
        """
        Abstract method that should be implemented in all subclass controllers, and should convert a given action
        into torques (pre gravity compensation) to be executed on the robot.
        Additionally, resets the self.new_update flag so that the next self.update call will occur
        """
        self.new_update = True

    def scale_action(self, action):
        """
        Clips @action to be within self.input_min and self.input_max, and then re-scale the values to be within
        the range self.output_min and self.output_max

        Args:
            action (Iterable): Actions to scale

        Returns:
            np.array: Re-scaled action
        """

        if self.action_scale is None:
            self.action_scale = abs(self.output_max - self.output_min) / abs(self.input_max - self.input_min)
            self.action_output_transform = (self.output_max + self.output_min) / 2.0
            self.action_input_transform = (self.input_max + self.input_min) / 2.0
        action = np.clip(action, self.input_min, self.input_max)
        transformed_action = (action - self.action_input_transform) * self.action_scale + self.action_output_transform

        return transformed_action

    def update(self, force=False):
        """
        Updates the state of the robot arm, including end effector pose / orientation / velocity, joint pos/vel,
        jacobian, and mass matrix. By default, since this is a non-negligible computation, multiple redundant calls
        will be ignored via the self.new_update attribute flag. However, if the @force flag is set, the update will
        occur regardless of that state of self.new_update. This base class method of @run_controller resets the
        self.new_update flag

        Args:
            force (bool): Whether to force an update to occur or not
        """

        # Only run update if self.new_update or force flag is set
        if self.new_update or force:
            self.sim.forward()

            self.ee_pos = np.array(self.sim.data.site_xpos[self.sim.model.site_name2id(self.eef_name)])
            self.ee_ori_mat = np.array(
                self.sim.data.site_xmat[self.sim.model.site_name2id(self.eef_name)].reshape([3, 3]))
            self.ee_pos_vel = np.array(self.sim.data.site_xvelp[self.sim.model.site_name2id(self.eef_name)])
            self.ee_ori_vel = np.array(self.sim.data.site_xvelr[self.sim.model.site_name2id(self.eef_name)])

            self.joint_pos = np.array(self.sim.data.qpos[self.qpos_index])
            self.joint_vel = np.array(self.sim.data.qvel[self.qvel_index])

            self.J_pos = np.array(self.sim.data.get_site_jacp(self.eef_name).reshape((3, -1))[:, self.qvel_index])
            self.J_ori = np.array(self.sim.data.get_site_jacr(self.eef_name).reshape((3, -1))[:, self.qvel_index])
            self.J_full = np.array(np.vstack([self.J_pos, self.J_ori]))

            mass_matrix = np.ndarray(shape=(len(self.sim.data.qvel) ** 2,), dtype=np.float64, order='C')
            mujoco_py.cymj._mj_fullM(self.sim.model, mass_matrix, self.sim.data.qM)
            mass_matrix = np.reshape(mass_matrix, (len(self.sim.data.qvel), len(self.sim.data.qvel)))
            self.mass_matrix = mass_matrix[self.qvel_index, :][:, self.qvel_index]

            # Clear self.new_update
            self.new_update = False

    def euler2EEangle_vel(self, ori_vel):
        """
        return ori_vel in body frame from euler frame.
        input: ori_vel from euler angle
        """
        euler_angle = T.mat2euler(self.ee_ori_mat)
        phi, theta, psi = euler_angle[0], euler_angle[1], euler_angle[2]

        mat_EE = np.array([[1, 0, np.sin(theta)],
                           [0, np.cos(phi), -np.cos(theta) * np.sin(phi)],
                           [0, np.sin(phi), np.cos(theta) * np.cos(phi)]])

        return np.dot(mat_EE, ori_vel)

    def euler2WorldAngle_vel(self, ori_vel):
        """
           return ori_vel in world frame from euler frame.
           input: ori_vel from euler angle
        """
        return np.dot(self.ee_ori_mat, self.euler2EEangle_vel(ori_vel))

    def update_base_pose(self, base_pos, base_ori):
        """
        Optional function to implement in subclass controllers that will take in @base_pos and @base_ori and update
        internal configuration to account for changes in the respective states. Useful for controllers e.g. IK, which
        is based on pybullet and requires knowledge of simulator state deviations between pybullet and mujoco

        Args:
            base_pos (3-tuple): x,y,z position of robot base in mujoco world coordinates
            base_ori (4-tuple): x,y,z,w orientation or robot base in mujoco world coordinates
        """
        pass

    def update_initial_joints(self, initial_joints):
        """
        Updates the internal attribute self.initial_joints. This is useful for updating changes in controller-specific
        behavior, such as with OSC where self.initial_joints is used for determine nullspace actions

        This function can also be extended by subclassed controllers for additional controller-specific updates

        Args:
            initial_joints (Iterable): Array of joint position values to update the initial joints
        """
        self.initial_joint = np.array(initial_joints)
        self.update(force=True)
        self.initial_ee_pos = self.ee_pos
        self.initial_ee_ori_mat = self.ee_ori_mat

    def clip_torques(self, torques):
        """
        Clips the torques to be within the actuator limits

        Args:
            torques (Iterable): Torques to clip

        Returns:
            np.array: Clipped torques
        """
        return np.clip(torques, self.actuator_min, self.actuator_max)

    def reset_goal(self):
        """
        Resets the goal -- usually by setting to the goal to all zeros, but in some cases may be different (e.g.: OSC)
        """
        raise NotImplementedError

    @staticmethod
    def nums2array(nums, dim):
        """
        Convert input @nums into numpy array of length @dim. If @nums is a single number, broadcasts it to the
        corresponding dimension size @dim before converting into a numpy array

        Args:
            nums (numeric or Iterable): Either single value or array of numbers
            dim (int): Size of array to broadcast input to env.sim.data.actuator_force

        Returns:
            np.array: Array filled with values specified in @nums
        """
        # First run sanity check to make sure no strings are being inputted
        if isinstance(nums, str):
            raise TypeError("Error: Only numeric inputs are supported for this function, nums2array!")

        # Check if input is an Iterable, if so, we simply convert the input to np.array and return
        # Else, input is a single value, so we map to a numpy array of correct size and return
        return np.array(nums) if isinstance(nums, Iterable) else np.ones(dim) * nums

    @property
    def torque_compensation(self):
        """
        Gravity compensation for this robot arm

        Returns:
            np.array: torques
        """
        return self.sim.data.qfrc_bias[self.qvel_index]

    @property
    def actuator_limits(self):
        """
        Torque limits for this controller

        Returns:
            2-tuple:

                - (np.array) minimum actuator torques
                - (np.array) maximum actuator torques
        """
        return self.actuator_min, self.actuator_max

    @property
    def control_limits(self):
        """
        Limits over this controller's action space, which defaults to input min/max

        Returns:
            2-tuple:

                - (np.array) minimum action values
                - (np.array) maximum action values
        """
        return self.input_min, self.input_max

    @property
    def name(self):
        """
        Name of this controller

        Returns:
            str: controller name
        """
        raise NotImplementedError

    def built_min_jerk_traj(self, t_finial, t_bias, via_point):
        """
        built minimum jerk (the desired trajectory)

        return:
        """
        pos_fi = via_point['p' + str(self.switch)]
        self.final_orientation = np.round(via_point['o' + str(self.switch)])

        pos_in = np.array(self.sim.data.site_xpos[self.sim.model.site_name2id(self.eef_name)])
        if self.switch == 1:
            pos_in[:2] = self.desired_vec_fin[-1][0][:2]
        self.initial_orientation = deepcopy(np.array(
            self.sim.data.site_xmat[self.sim.model.site_name2id(self.eef_name)].reshape([3, 3])))

        if self.method == 'euler':
            self.fin_orientation = T.mat2euler(self.final_orientation)
            if self.switch == 1:
                self.init_orientation = self.desired_vec_fin[-1][1]
            else:
                self.init_orientation = T.mat2euler(self.initial_orientation)
                # self.init_orientation[2] = abs(self.init_orientation[2])
                # self.init_orientation[0] = abs(self.init_orientation[0])

        self.X_init = pos_in[0]
        self.Y_init = pos_in[1]
        self.Z_init = pos_in[2]

        self.X_final = pos_fi[0]
        self.Y_final = pos_fi[1]
        self.Z_final = pos_fi[2]

        self.t_finial = t_finial
        self.t_bias = t_bias

    def built_next_desired_point(self):

        t = self.sim.data.time - self.t_bias

        x_traj = (self.X_final - self.X_init) / (self.t_finial ** 3) * (
                6 * (t ** 5) / (self.t_finial ** 2) - 15 * (t ** 4) / self.t_finial + 10 * (t ** 3)) + self.X_init
        y_traj = (self.Y_final - self.Y_init) / (self.t_finial ** 3) * (
                6 * (t ** 5) / (self.t_finial ** 2) - 15 * (t ** 4) / self.t_finial + 10 * (t ** 3)) + self.Y_init
        z_traj = (self.Z_final - self.Z_init) / (self.t_finial ** 3) * (
                6 * (t ** 5) / (self.t_finial ** 2) - 15 * (t ** 4) / self.t_finial + 10 * (t ** 3)) + self.Z_init
        position = np.array([x_traj, y_traj, z_traj])

        # velocities
        vx = (self.X_final - self.X_init) / (self.t_finial ** 3) * (
                30 * (t ** 4) / (self.t_finial ** 2) - 60 * (t ** 3) / self.t_finial + 30 * (t ** 2))
        vy = (self.Y_final - self.Y_init) / (self.t_finial ** 3) * (
                30 * (t ** 4) / (self.t_finial ** 2) - 60 * (t ** 3) / self.t_finial + 30 * (t ** 2))
        vz = (self.Z_final - self.Z_init) / (self.t_finial ** 3) * (
                30 * (t ** 4) / (self.t_finial ** 2) - 60 * (t ** 3) / self.t_finial + 30 * (t ** 2))
        velocity = np.array([vx, vy, vz])

        # acceleration
        ax = (self.X_final - self.X_init) / (self.t_finial ** 3) * (
                120 * (t ** 3) / (self.t_finial ** 2) - 180 * (t ** 2) / self.t_finial + 60 * t)
        ay = (self.Y_final - self.Y_init) / (self.t_finial ** 3) * (
                120 * (t ** 3) / (self.t_finial ** 2) - 180 * (t ** 2) / self.t_finial + 60 * t)
        az = (self.Z_final - self.Z_init) / (self.t_finial ** 3) * (
                120 * (t ** 3) / (self.t_finial ** 2) - 180 * (t ** 2) / self.t_finial + 60 * t)
        acceleration = np.array([ax, ay, az])

        # orientation
        if self.method == 'rotation':
            Vrot = T.Rotation_Matrix_To_Vector(self.initial_orientation, self.final_orientation)

            upper_bound = 1e-6
            if np.linalg.norm(Vrot) < upper_bound:
                magnitude_traj = 0.0
                magnitude_vel_traj = 0.0
                direction = np.array([0.0, 0.0, 0.0])
            else:
                magnitude, direction = T.Axis2Vector(Vrot)
                #   we want to decrease the magnitude of the rotation from some initial value to 0
                magnitude_traj = magnitude / (self.t_finial ** 3) * (
                        6 * (t ** 5) / (self.t_finial ** 2) - 15 * (t ** 4) / self.t_finial + 10 * (t ** 3)) - magnitude
                magnitude_vel_traj = magnitude / (self.t_finial ** 3) * (
                        30 * (t ** 4) / (self.t_finial ** 2) - 60 * (t ** 3) / self.t_finial + 30 * (t ** 2))

            orientation = magnitude_traj * direction
            ang_vel = magnitude_vel_traj * direction

        if self.method == 'euler':
            orientation = (self.fin_orientation - self.init_orientation) / (self.t_finial ** 3) * (
                    6 * (t ** 5) / (self.t_finial ** 2) - 15 * (t ** 4) / self.t_finial + 10 * (t ** 3)) + self.init_orientation
            ang_vel = (self.fin_orientation - self.init_orientation) / (self.t_finial ** 3) * (
                    30 * (t ** 4) / (self.t_finial ** 2) - 60 * (t ** 3) / self.t_finial + 30 * (t ** 2))

        self.desired_vec_fin.append([position, orientation, velocity, ang_vel])
        return position, orientation, velocity, ang_vel