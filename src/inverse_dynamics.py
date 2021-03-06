#!/usr/bin/env python
import rospy
import numpy as np
from geometry_msgs.msg import TwistStamped, PoseStamped
from delta_2.msg import ServoAngles6DoFStamped
from tf.transformations import euler_from_quaternion
from message_filters import ApproximateTimeSynchronizer, Subscriber

#code to determine servo velocities from end effector velocity setpoints
#todo: there is a singularity when the platform is level (phi and theta are aligned), maybe quaternions can fix this?

class InverseDynamics:
    def __init__(self):
        robot_name = "delta_2"

        #get geometry values form parameter server
        rp = rospy.get_param('/' + robot_name + '/base_radius')
        rb = rospy.get_param('/' + robot_name + '/platform_radius')
        sb = rospy.get_param('/' + robot_name + '/base_joint_spacing')
        sp = rospy.get_param('/' + robot_name + '/platform_joint_spacing')
        self.ra = rospy.get_param('/' + robot_name + '/proximal_link_length')
        self.rs = rospy.get_param('/' + robot_name + '/distal_link_length')

        #angles of servo motors about centre of base
        self.beta = np.asarray([np.deg2rad(30), np.deg2rad(30), np.deg2rad(150), np.deg2rad(150), np.deg2rad(270), np.deg2rad(270)])

        #calculate coordinates of wrist and shoulder joints in their respective body coordinates
        c30 = np.cos(np.deg2rad(30))
        s30 = np.sin(np.deg2rad(30))

        #platform joints in platform coordinates
        self.p_p = np.asarray([[rp * c30 + sp * s30, rp * s30 - sp * c30, 0],
            [rp * c30 - sp * s30, rp * s30 + sp * c30, 0],
            [-rp * c30 + sp * s30, rp * s30 + sp * c30, 0],
            [-rp * c30 - sp * s30, rp * s30 - sp * c30, 0],
            [-sp, -rp, 0],
            [sp, -rp, 0]])

        #base joints in base/world coordinates
        self.b_w = np.asarray([[rb * c30 + sb * s30, rb * s30 - sb * c30, 0],
            [rb * c30 - sb * s30, rb * s30 + sb * c30, 0],
            [-rb * c30 + sb * s30, rb * s30 + sb * c30, 0],
            [-rb * c30 - sb * s30, rb * s30 - sb * c30, 0],
            [-sb, -rb, 0],
            [sb, -rb, 0]])

        #initialise Jacobean
        self.J2_inv = np.identity(6)

        #init publisher and subscriber
        self.pub_servo_velocities = rospy.Publisher('/' + robot_name + '/servo_setpoint/velocities', ServoAngles6DoFStamped, queue_size=1) #servo velocity publisher
        sub_platform_vel = Subscriber('/' + robot_name + '/platform_setpoint/twist', TwistStamped) #target twist subscriber
        sub_platform_pose = Subscriber('/' + robot_name + '/platform_setpoint/pose', PoseStamped) #target pose subscriber
        sub_servo_angles = Subscriber('/' + robot_name + '/servo_setpoint/positions', ServoAngles6DoFStamped) #servo angle subscriber

        ts = ApproximateTimeSynchronizer([sub_platform_vel, sub_platform_pose, sub_servo_angles], queue_size=1, slop=0.05)
        ts.registerCallback(self.vel_callback)

    def vel_callback(self, platform_vel, platform_pose, servo_angles):
        #initialise empty numpy arrays
        p_w = np.zeros((6,3))
        L_w = np.zeros((6,3))
        n = np.zeros((6,3))
        p = np.zeros((6,3))
        J1_inv = np.zeros((6,6))
        Theta_dot = np.zeros(6)
        M = np.zeros(6)
        N = np.zeros(6)

        #put calculated servo angles in array
        Theta = np.asarray([np.deg2rad(servo_angles.Theta1), 
                                np.deg2rad(servo_angles.Theta2),
                                np.deg2rad(servo_angles.Theta3), 
                                np.deg2rad(servo_angles.Theta4), 
                                np.deg2rad(servo_angles.Theta5), 
                                np.deg2rad(servo_angles.Theta6)])

        #convert quaternion to Euler angles
        (psi, theta, phi) = euler_from_quaternion([platform_pose.pose.orientation.x, platform_pose.pose.orientation.y, platform_pose.pose.orientation.z, platform_pose.pose.orientation.w])

        #Q is the target state vector (x,y,z,phi,psi,theta)
        Q = np.asarray([platform_pose.pose.position.x, platform_pose.pose.position.y, platform_pose.pose.position.z, phi, psi, theta])
        
        #assign rotations to rotation matrix wRp (from platform to world coordinates)
        cphi = np.cos(phi)
        sphi = np.sin(phi)
        cpsi = np.cos(psi)
        spsi = np.sin(psi)
        ctheta = np.cos(theta)
        stheta = np.sin(theta)

        Rpsi = np.asarray([[cpsi, -spsi, 0],
                            [spsi, cpsi, 0],
                            [0, 0, 1]])
    
        Rtheta = np.asarray([[1, 0, 0],
                            [0, ctheta, -stheta],
                            [0, stheta, ctheta]])
      
        Rphi = np.asarray([[cphi, 0, sphi],
                            [0, 1, 0],
                            [-sphi, 0, cphi]])

        wRp = np.matmul(np.matmul(Rpsi, Rtheta), Rphi)

        #angular velocities
        omega = np.asarray([[0, cpsi, spsi * stheta],
                [0, spsi, -cpsi * stheta],
                [1, 0, ctheta]])
        
        #determine J2_inv
        self.J2_inv[3:6, 3:6] = omega
   
        #assign velocities to vector q_dot
        Q_dot = np.asarray([platform_vel.twist.linear.x, platform_vel.twist.linear.y, platform_vel.twist.linear.z,
                            platform_vel.twist.angular.x, platform_vel.twist.angular.y, platform_vel.twist.angular.z])
        
        for i in range(6):
            #the following 4 lines are repeated from inverse kinematics to obtain useful intermediate variables
            p_w[i,:] = Q[0:3] + np.matmul(wRp, self.p_p[i,:])
            L_w[i,:] = p_w[i,:] - self.b_w[i,:]
            M[i] = 2 * self.ra * p_w[i,2]
            N[i] = 2 * self.ra * (np.cos(self.beta[i]) * (p_w[i,0] - self.b_w[i,0]) + np.sin(self.beta[i]) * (p_w[i,1] - self.b_w[i,1]))

            #this is the new stuff to calculate velocities
            n[i,:] = L_w[i,:] / np.linalg.norm(L_w[i,:])
            w = np.cross(self.p_p[i,:], n[i,:])
            p[i,:] = np.matmul(wRp, w)
        
        J1_inv = np.column_stack((n, p))
        L_w_dot = np.matmul(np.matmul(J1_inv, self.J2_inv), Q_dot)

        Theta_dot = L_w_dot / (M * np.cos(Theta) - N * np.sin(Theta))

        #publish
        servo_velocities = ServoAngles6DoFStamped()
        servo_velocities.header.frame_id = "servo"
        servo_velocities.header.stamp = platform_pose.header.stamp
        servo_velocities.Theta1 = np.rad2deg(Theta_dot[0])
        servo_velocities.Theta2 = np.rad2deg(Theta_dot[1])
        servo_velocities.Theta3 = np.rad2deg(Theta_dot[2])
        servo_velocities.Theta4 = np.rad2deg(Theta_dot[3])
        servo_velocities.Theta5 = np.rad2deg(Theta_dot[4])
        servo_velocities.Theta6 = np.rad2deg(Theta_dot[5])
        self.pub_servo_velocities.publish(servo_velocities)

if __name__ == '__main__': #initialise node and run loop
    rospy.init_node('inverse_dynamics')
    id = InverseDynamics()
    rospy.spin()