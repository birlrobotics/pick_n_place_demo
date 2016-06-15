#!/usr/bin/env python
import ipdb
import sys
import argparse
import rospy
from math import pi 

import baxter_interface
from baxter_interface import CHECK_VERSION
from hand_action import GripperClient
from arm_action import (computerIK, computerApproachPose)

from pa_localization.msg import pa_location
from birl_recorded_motions import paHome_rightArm as rh
from copy import copy
import PyKDL
import tf

import threading

class getPickingPose():
    def __init__(self):
        # Subscribe the pickingPose from pa_localization
        self._pose=pa_location()
        self._lock=threading.Lock()
        self._thread=threading.Thread(target=self.picking_location_listener)
        self._thread.start()

    def picking_location_listener(self):
        rospy.Subscriber("pick_location",pa_location,self.callback)
        rospy.spin()

    def callback(self,msg):
        self.lock()
        self._pose=msg
        self.unlock()

    def getPose(self):
        return self._pose

    def lock(self):
        self._lock.acquire()

    def unlock(self):
        self._lock.release()

def main():
    # Two globals: reference_origin_pose=the origin of the world. We'll use that from the visual detection and add an x,y offset.
    # reference_placing_pose_flag=currently pre-recorded location(s) to place the target object.
    global reference_origin_pose_flag, reference_placing_pose_flag

    arg_fmt = argparse.RawDescriptionHelpFormatter # create ArgumentParser object
    parser = argparse.ArgumentParser(formatter_class=arg_fmt,
                                     description=main.__doc__)
    required = parser.add_argument_group('required arguments') # set required strings
    required.add_argument(
        '-l', '--limb', required=True, choices=['left', 'right'],
        help='send joint trajectory to which limb')
    args = parser.parse_args(rospy.myargv()[1:]) # return objects

    # Set limb and gripper side
    limb = args.limb
    ha=GripperClient(limb)

    pp_thread=getPickingPose()

    # get robot State
    print("Getting robot state...")
    rs=baxter_interface.RobotEnable(CHECK_VERSION)
    print("Enabling robot...")
    rs.enable()
    print("Running. Ctrl-c to quit")

    # Create Joint name list
    jNamesList=['right_s0','right_s1','right_e0','right_e1','right_w0','right_w1','right_w2']

	# Create the arm object to move the arm
    arm=baxter_interface.limb.Limb(limb)
    current_js=[arm.joint_angle(joint) for joint in arm.joint_names()]

    rospy.loginfo('Starting pa demo...')

	# Step 1: Go to home position and open gripper to be ready to pick.
    rospy.loginfo('I will first move to home position, and open the gripper...')
    rh.paHome_rightArm()
    ha.open()
    rospy.sleep(1.0)

    # Instantiate the IK and computeApproach objects with the correct limb.
    kin=computerIK(limb)
    cal_pose=computerApproachPose(limb)

    #-------------- HARD-CODED Origin for Baxter using pa_localization on a 2D Plane.

    x= 0.543529946547
    y= -0.458792031781
    z= -0.132623004553
    qx= 0.714250254661
    qy= -0.699035963527
    qz= 0.0345305451586
    qw= 0.00171372790659
    #---------------------------------------------------------------------------------------------

    # Set the origin as a dictionary with position and orientation
    origin_p=baxter_interface.limb.Limb.Point(x,y,z)
    origin_q=baxter_interface.limb.Limb.Quaternion(qx,qy,qz,qw)
    origin_pose={'position':origin_p, 'orientation':origin_q}

    # Get Starting Position
    startPose=arm.endpoint_pose()
    startJoints=kin.calIK_PY_KDL(startPose)

    while not rospy.is_shutdown():

        # Put the male part on the table
        rospy.loginfo('Please put the male part on the table, and I will capture the location for picking')
        key=raw_input('When finished, press any key...')

        rospy.loginfo("waiting for picking pose from camera...")
        rospy.sleep(2.0)
        pp_thread.lock()
        picking_pose_visual=pp_thread.getPose()
        pp_thread.unlock()
        print(picking_pose_visual)

        #---------------------------------------------------------------------------------------------------
        #  pa_localization
		#  This section of the code implements a pick and place operation. We are picking boxes.
		#  The program will acquire a "offset" x,y,theta location relative to an origin designated
		#  by the pa_localization calibration routine. The arm, as seen above, has a reference "origin"
		#  pose position.
		#  
		#  The first step of the program is to pick, the second step is to place.
		#
		#  PICKING:
		#  To pick, we need to obtain the offset visual location and then apply the offset to the 
		#  starting pose of the robot. For simplicity, the way we approach a target pose in this program
		#  is by moving the arm to 2 points above the target pose. The first point will be some distance z1
		#  above the target pose, say 5cm, the 2nd point is closer, z2=1cm above the target pose.
		#  After that, we go for a pick from the top.
		# 
		#  PLACING:
		#  For placing, the placing goal pose is hard-coded (this could easily be solved with the 
		#  current pa_localization program asking to recognize the goal poses...).
		#  The robot will move to the placing location along with the two step approach mentioned above.
        #    require: reference_origin_pose
        #    return:  picking_pose---{'position': picking_p, 'orientation': picking_q}
        #---------------------------------------------------------------------------------------------------

		# Set the origin pose as the picking pose or the reference pose.
        picking_pose=copy(origin_pose) # referring picking_pose from pa_localization to origin

        ref_x=copy(picking_pose['position'][0])
        ref_y=copy(picking_pose['position'][1])
        ref_z=copy(picking_pose['position'][2])

        ref_q=picking_pose['orientation']
        rot_mat=PyKDL.Rotation.Quaternion(ref_q.x,ref_q.y,ref_q.z,ref_q.w)
        rot_rpy=rot_mat.GetRPY()
        rot_rpy=list(rot_rpy)

		# Now add the offset from the visual system
        p_x=ref_x+picking_pose_visual.y
        p_y=ref_y+picking_pose_visual.x
        p_z=ref_z
        q=tf.transformations.quaternion_from_euler(rot_rpy[0],rot_rpy[1],rot_rpy[2]-picking_pose_visual.angle,axes='sxyz').tolist()
        picking_p=baxter_interface.limb.Limb.Point(p_x,p_y,p_z)
        picking_q=baxter_interface.limb.Limb.Quaternion(q[0],q[1],q[2],q[3])
        picking_pose={'position': picking_p, 'orientation': picking_q}

        #--------------- Picking Process -----------------------
        # Calculate the approach pose for the picking process. It generates two points befor pick.
        approach_J_1,approach_J_2,pickingJoints=cal_pose.get_approach_joints_2(picking_pose,[0,0,0.05],[0,0,0.01])

        rospy.loginfo('Start picking...')
        arm.move_to_joint_positions(approach_J_1)
        rospy.sleep(1.0)
        print(arm.endpoint_pose())
        arm.move_to_joint_positions(approach_J_2)
        rospy.sleep(2.0)
        print(arm.endpoint_pose())
        arm.move_to_joint_positions(pickingJoints)
        rospy.sleep(2.0)
        print(arm.endpoint_pose())

        # close gripper
        ha.close()
        rospy.sleep(1.0)

		# Lift up picked object.
        rospy.loginfo('Lift up...')
        arm.move_to_joint_positions(approach_J_2)
        rospy.sleep(1.0)
        arm.move_to_joint_positions(approach_J_1)
        rospy.sleep(1.0)

        #-------------------- HARD-CODED Placing Pose for small male box in pa_demo --------------
        x= 0.545716771422
        y= -0.0120040535129
        z= -0.129033789691
        qx= 0.717703731308
        qy= -0.695809907218
        qz= 0.0273082981745
        qw= -0.00204546698183

        # x= 0.551502038059
        # y=-0.0176207852061
    	# z=-0.134496202287
    	# qx= 0.721767501695
    	# qy= -0.691790773243
    	# qz= 0.00497860983363
    	# qw= -0.0212700022811
        #-----------------------------------------------------------------------------------------

        placing_p=baxter_interface.limb.Limb.Point(x,y,z)
        placing_q=baxter_interface.limb.Limb.Quaternion(qx,qy,qz,qw)
        placing_pose={'position':placing_p, 'orientation':placing_q}

        #--------------- Placing Process -----------------------
        # Calculate approach phase for placing action. 2 steps ahead of place. 
        approach_J_1,approach_J_2,placingJoints=cal_pose.get_approach_joints_2(placing_pose,[0.0,0.0,0.05],[0.0,0.0,0.015p])

        rospy.loginfo('Start placing...')
        arm.move_to_joint_positions(approach_J_1)
        rospy.sleep(2.0)
        print(arm.endpoint_pose())
        arm.move_to_joint_positions(approach_J_2)
        rospy.sleep(3.0)
        print(arm.endpoint_pose())
        arm.move_to_joint_positions(placingJoints)
        rospy.sleep(2.0)
        print(arm.endpoint_pose())

        # open gripper
        ha.open()
        rospy.sleep(1.0)

        #------------------------- HARD-CODED MidPosition for in between pick n place --------------------------
    	x= 0.547189372077
    	y= -0.0259615652387
    	z= -0.0808804442523
    	qx= -0.697759568783
    	qy= 0.709338547384
    	qz= 0.00848900483209
    	qw= 0.0994904325274
        #-------------------------------------------------------------------------------------------------------

		# The midpoint is an intermediate step of where you could go before startin again. 
        mid_p=baxter_interface.limb.Limb.Point(x,y,z)
        mid_q=baxter_interface.limb.Limb.Quaternion(qx,qy,qz,qw)
        mid_pose={'position':mid_p, 'orientation':mid_q}

        midJoints=kin.calIK_PY_KDL(mid_pose)
        arm.move_to_joint_positions(midJoints)
        rospy.sleep(2.0)
        print(arm.endpoint_pose())

        rospy.loginfo('Move to home position...')
        rh.paHome_rightArm()
        rospy.sleep(1.0)

if __name__ == "__main__":
    rospy.init_node("pa_manipulation")
    main()
