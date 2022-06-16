import os
import os.path
import pybullet as p
import torch
import sys
import math
from yacs.config import CfgNode as CN
import copy
import random
import trimesh
import time

cn_path = os.path.join(os.getenv('HOME'), 'pytorch_contactnet/')
sys.path.append(cn_path)
ar_path = os.path.join(os.getenv('HOME'), 'airobot/src/')
sys.path.append(ar_path)
ik_path = os.path.join(os.getenv('HOME'), 'pybullet-planning/')
sys.path.append(ik_path)

import numpy as np

from model.contactnet import ContactNet
import model.utils.config_utils as config_utils
from dataset import ContactDataset
from train import initialize_loaders, initialize_net
from scipy.spatial.transform import Rotation as R
import argparse
from torch_geometric.nn import fps
from test_meshcat_pcd import viz_pcd as V

from franka_ik import FrankaIK
from panda_pb_cfg import get_cfg_defaults
from eval_gen_utils import safeCollisionFilterPair, soft_grasp_close, constraint_grasp_close, constraint_grasp_open, object_is_still_grasped

import airobot
from airobot import Robot


class PandaPB():

    def __init__(self, model, dataset, config, gui=True):

        panda_cfg = get_cfg_defaults()
        self.pb_robot = Robot('franka',
                          pb_cfg={'gui': gui},
                          arm_cfg={'self_collision': False,
                                   'seed': None})

        self.ik_robot = FrankaIK(gui=False, base_pos=[-0.3, 0.3, 0])
        self.pb_robot.pb_client.set_step_sim(True)

        self.pb_robot.cam.setup_camera(
            focus_pt=[0, 0, 0],
            dist=1.5, yaw=0, pitch=-40)

        self.model = model
        self.dataset = dataset
        self.config = config
        self.pb_ids = []

        left_pad_id = 9
        right_pad_id = 10
        p.changeDynamics(self.pb_robot.arm.robot_id, left_pad_id, lateralFriction=1.0)
        p.changeDynamics(self.pb_robot.arm.robot_id, right_pad_id, lateralFriction=1.0)
        
    def get_rand_scene(self, scene_idx):
        '''
        Returns a random scene from the ShapeNet generated scenes folder
        '''
        if scene_idx is None:
            scene_idx = np.random.randint(0, len(self.dataset.data))
        data_file = self.dataset.data[scene_idx]
        filename = '../acronym/scene_contacts/' + os.fsdecode(data_file)
        scene_data = np.load(filename, allow_pickle=True)
        obj_paths = scene_data['obj_paths']
        for i, path in enumerate(obj_paths):
            fixed_path = '../acronym/models/' + path.split('/')[-1]
            obj_paths[i] = fixed_path
        obj_scales = scene_data['obj_scales']
        obj_transforms = scene_data['obj_transforms']
        return obj_scales, obj_transforms, obj_paths

    def create_pb(self, path, scale, transform, rgba, specular, collision=False, fix_name=None):
        '''
        Loads up set of meshes and creates pybullet collision + visual for them
        '''
        collision_args = {}
        visual_args = {}
        t = np.array(transform[:3,3]).T
        rot_mat = np.array(transform[:3, :3])
        q = R.from_matrix(rot_mat).as_quat()

        tmesh = trimesh.load(path, process=False)
        if isinstance(tmesh, trimesh.Scene):
            tmesh_merge = trimesh.util.concatenate(
                    tuple(trimesh.Trimesh(vertices=g.vertices, faces=g.faces)
                        for g in tmesh.geometry.values()))
            tmp_path = os.path.join(os.getcwd(), fix_name)
        else:
            tmesh_merge = tmesh
            tmp_path = path

        tmesh_merge.vertices -= tmesh.centroid
        tmesh_merge.export(tmp_path)
            
        collision_args['collisionFramePosition'] = None
        collision_args['collisionFrameOrientation'] = None
        visual_args['visualFramePosition'] = None
        visual_args['visualFrameOrientation'] = None

        collision_args['shapeType'] = p.GEOM_MESH
        collision_args['fileName'] = tmp_path
        collision_args['meshScale'] = np.array([1,1,1])*scale
        visual_args['shapeType'] = p.GEOM_MESH
        visual_args['fileName'] = tmp_path
        visual_args['meshScale'] = np.array([1,1,1])*scale
        visual_args['rgbaColor'] = rgba
        visual_args['specularColor'] = specular
        '''
        vs_id = p.createVisualShape(**visual_args)
        cs_id = p.createCollisionShape(**collision_args)
        body_id = p.createMultiBody(baseMass=1.0,
                                       baseInertialFramePosition=None,
                                       baseInertialFrameOrientation=None,
                                       baseCollisionShapeIndex=cs_id,
                                       baseVisualShapeIndex=vs_id,
                                       basePosition=t,
                                       baseOrientation=q)
                                       #**kwargs)
        '''
        if collision:
            #self.pb_ids.append(body_id)
            obj_id = self.pb_robot.pb_client.load_geom(shape_type='mesh', visualfile=tmp_path, collifile=tmp_path,
                                     mass=1.0, mesh_scale=scale, rgba=rgba, specular=specular,
                                     base_pos=t, base_ori=q)
            p.changeDynamics(obj_id, -1, lateralFriction=0.5)
            p.changeDynamics(obj_id, -1, linearDamping=5, angularDamping=5)
            time.sleep(1.5)
            self.ik_robot.add_collision_bodies({tmp_path:obj_id})
            rid = self.pb_robot.arm.robot_id
            for i in range(p.getNumJoints(rid)):
                safeCollisionFilterPair(bodyUniqueIdA=rid, bodyUniqueIdB=obj_id, linkIndexA=i, linkIndexB=-1, enableCollision=True,
                                        physicsClientId=self.pb_robot.pb_client.get_client_id())
            print(obj_id)

        
    def generate_scene(self, scene_index=None):
        '''
        Creates a mesh scene in pybullet from random dataset scene
        Returns: point cloud for inference
        '''
        self.model.eval()
        # Get a random cluttered scene from the dataset
        obj_scales, obj_transforms, obj_paths = self.get_rand_scene(scene_index)

        # Pybullet set up
        p.setGravity(0, 0, -9.8)

        # Instantiate the objects in the scene
        i = 0
        for path, scale, transform in zip(obj_paths, obj_scales, obj_transforms):
            name = 'object_' + str(i) + '.obj'
            i += 1
            transform[2, 3] -= 0.3
            body_id = self.create_pb(path, scale, transform,
                                     rgba = [0.5, 0, 0, 1],
                                     specular = [0, 0.5, 0.4],
                                     collision=True,
                                     fix_name=name)

        self.pb_robot.pb_client.set_step_sim(False)
        time.sleep(5) #allow objects to settle
        
        # Render pointcloud
        rgb, depth, seg = self.pb_robot.cam.get_images(
                    get_rgb=True,
                    get_depth=True,
                    get_seg=True)
        pcd, colors_raw = self.pb_robot.cam.get_pcd(
            in_world=True,
            filter_depth=True,
            depth_min=-5.0,
            depth_max=5.0)

        # Get segmented pointclouds
        seg = seg.reshape(-1)
        seg_ids = np.unique(seg)
        seg_ids = np.delete(seg_ids, 0)
        seg_masks = np.zeros((seg.shape[0], 0))
        for seg_id in seg_ids:
            mask = np.array([seg==seg_id]).T
            seg_masks = np.concatenate((seg_masks, mask), axis=1)

            m = np.nonzero(seg==seg_id)

            V(pcd[m[0], :], str(seg_id))

        # Crop pointcloud and potentially center it as well
        x_mask = np.where((pcd[:, 0] < 0.5) & (pcd[:, 0] > -0.5))
        y_mask = np.where((pcd[:, 1] < 0.5) & (pcd[:, 1] > -0.5))
        z_mask = np.where((pcd[:, 2] < 0.5)) # & (pcd[:, 2] > 0.04))
        mask = np.intersect1d(x_mask, y_mask)
        mask = np.intersect1d(mask, z_mask)
        pcd = pcd[mask]
        seg_masks = seg_masks[mask]

        print('segmentation', seg_ids, seg_masks)
        return pcd, seg_ids, seg_masks

    def infer(self, args, pcd=None):
        '''
        Runs inference on point cloud
        If no point cloud provided, will get one from rand scene in dataset
        '''
        if pcd is None:
            pcd, seg_ids, seg_masks = self.generate_scene(args.scene)
            
        # Forward pass into model
        downsample = np.array(random.sample(range(pcd.shape[0]-1), 20000))
        pcd = pcd[downsample, :]
        seg_masks = seg_masks[downsample, :]
        V(pcd, 'pb_raw', clear=True)

        pcd = torch.Tensor(pcd).to(dtype=torch.float32).to(self.model.device)
        batch = torch.ones(pcd.shape[0]).to(dtype=torch.int64).to(self.model.device)
        idx = torch.linspace(0, pcd.shape[0]-1, 2048).to(dtype=torch.int64).to(self.model.device) #fps(pcd, batch, 2048/pcd.shape[0])
        seg_masks = seg_masks[idx.cpu().numpy(), :]
        points, pred_grasps, pred_successes, pred_widths = self.model(pcd[:, 3:], pos=pcd[:, :3], batch=batch, idx=idx, k=None)
        
        print('model pass')
        pred_grasps = torch.flatten(pred_grasps, start_dim=0, end_dim=1)
        pred_successes = torch.flatten(pred_successes)
        pred_widths = torch.flatten(pred_widths, start_dim=0, end_dim=1)
        points = torch.flatten(points, start_dim=0, end_dim=1)

        pcd = pcd.detach().cpu().numpy()

        '''
        # Return a sample of predicted grasps
        pcd = pcd[idx.detach().cpu().numpy()]
        obj_mask = np.where(pcd[:, 2] > 0.04)[0]
        print('object mask is', obj_mask.shape)
        obj_success = pred_successes[obj_mask]
        success, sample_idx = torch.topk(obj_success, 10)
        V(points.detach().cpu().numpy()[obj_mask], 'masked')
        sample_idx = obj_mask[sample_idx.detach().cpu().numpy()]

        point = points[sample_idx[:10]]
        top_grasp = pred_grasps[sample_idx]
        grasp_width = pred_widths[sample_idx]

        point = point.detach().cpu().numpy()
        top_grasp = top_grasp.detach().cpu().numpy()
        '''
        top_grasp = pred_grasps.detach().cpu().numpy()
        grasp_width = pred_widths.detach().cpu().numpy()
        point = points.detach().cpu().numpy()
        pred_successes = pred_successes #.detach().cpu().numpy()
        
        return top_grasp, grasp_width, point, pred_successes, seg_ids, seg_masks #torch.max(pred_successes).detach().cpu()

    def viz_predictions(self, grasps, points):
        '''
        visualizes predictions as a semi-transparent gripper and point
        '''
        for grasp, point in zip(grasps, points):
            panda_path = './gripper_models/panda_gripper/panda_gripper.obj'
            self.create_pb(panda_path, 1.0, grasp,
                           rgba = [0, 0, 0, 0.5],
                           specular = [0, 0.5, 0.4])
            q = [0,0,0,1]
            t = point

            collision_args = {}
            visual_args = {}
            collision_args['collisionFramePosition'] = None
            collision_args['collisionFrameOrientation'] = None
            visual_args['visualFramePosition'] = None
            visual_args['visualFrameOrientation'] = None
            collision_args['shapeType'] = p.GEOM_SPHERE
            collision_args['radius'] = 0.01
            visual_args['shapeType'] = p.GEOM_SPHERE
            visual_args['radius'] = 0.01
            visual_args['rgbaColor'] = [0, 0, 0, 0.5]
            visual_args['specularColor'] = [0, 0.5, 0.4]

            vs_id = p.createVisualShape(**visual_args)
            cs_id = p.createCollisionShape(**collision_args)
            body_id = p.createMultiBody(baseMass=1.0,
                                           baseInertialFramePosition=None,
                                           baseInertialFrameOrientation=None,
                                           baseCollisionShapeIndex=cs_id,
                                           baseVisualShapeIndex=vs_id,
                                           basePosition=t,
                                           baseOrientation=q)
                                           #**kwargs)
            
    def pb_execute(self, args):
        '''
        Wrapper for rendering, inference, IK, and execution
        '''
        predicted_grasps, predicted_widths, scene_points, pred_s, seg_ids_raw, seg_masks = self.infer(args)
        #self.viz_predictions(predicted_grasps, points)

        # Separate grasps by segmentation masks
        grasps = []
        widths = []
        points = []
        success = []
        seg_ids = []
        for i in range(len(seg_ids_raw)):
            mask = np.nonzero(seg_masks[:, i])
            if len(pred_s[mask]) != 0 and i!=0:
                grasps.append(predicted_grasps[mask])
                widths.append(predicted_grasps[mask])
                points.append(scene_points[mask])
                success.append(pred_s[mask])
                seg_ids.append(seg_ids_raw[i])
            else:
                print('rejecting', seg_ids_raw[i])
            
        for obj_grasps, obj_widths, obj_points, s, seg_id in zip(grasps, widths, points, success, seg_ids):
            success, sample_idx = torch.topk(s, 2)
            sample_idx = sample_idx.detach().cpu().numpy()
            obj_grasps = obj_grasps[sample_idx]
            obj_widths = obj_widths[sample_idx]
            obj_points = obj_points[sample_idx]
            print('object:', seg_id)
            for grasp, width, point in zip(obj_grasps, obj_widths, obj_points):
                # rotate grasp by pi/2 about the z axis (urdf fix) and change to grasp target
                z_r = R.from_euler('z', np.pi/2, degrees=False)
                z_rot = np.eye(4)
                z_rot[:3,:3] = z_r.as_matrix()
                z_rot = np.matmul(z_rot, np.linalg.inv(grasp))
                z_rot = np.matmul(grasp, z_rot)
                grasp = np.matmul(z_rot, grasp)

                lift_pose = copy.deepcopy(grasp)
                lift_pose[2, 3] += 0.3
                lift_pos = lift_pose[:3, 3]

                rot = R.from_matrix(grasp[:3, :3])
                quat = rot.as_quat()
                pos = grasp[:3, -1]

                self.pb_robot.pb_client.set_step_sim(False)
                self.pb_robot.arm.go_home(ignore_physics=True)
                self.pb_robot.arm.reset()
                time.sleep(1)

                pose = tuple([*pos, *quat])
                lift_pose = tuple([*lift_pos, *quat])
                sol_jnts = self.ik_robot.get_feasible_ik(pose, target_link=False)
                lift_jnts = self.ik_robot.get_feasible_ik(lift_pose, target_link=False)
                if sol_jnts is None or lift_jnts is None:
                    continue

                time.sleep(1)
                self.pb_robot.arm.eetool.open(ignore_physics=True)

                time.sleep(1)
                self.pb_robot.arm.set_jpos(sol_jnts, wait=True, ignore_physics=True)
                soft_grasp_close(self.pb_robot, 9, force=50)
                soft_grasp_close(self.pb_robot, 10, force=50)
                time.sleep(2)
                print('solution is', sol_jnts)
                cid = constraint_grasp_close(self.pb_robot, seg_id)
                self.pb_robot.arm.set_jpos(lift_jnts, wait=True)
                time.sleep(1.0)

                grasp_success = object_is_still_grasped(self.pb_robot, seg_id, 9, 10)
                print('object', seg_id, grasp_success)

                constraint_grasp_open(cid)
                self.pb_robot.arm.eetool.open()

                success = False
            

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--save_path', type=str, default='./checkpoints/single.pth', help='path to load model from')
    parser.add_argument('--config_path', type=str, default='./model/', help='path to config yaml file')
    parser.add_argument('--data_path', type=str, default='/home/alinasar/acronym/scene_contacts', help='path to acronym dataset with Contact-GraspNet folder')
    parser.add_argument('--scene', type=int, default=0)
    args = parser.parse_args()

    contactnet, optim, config = initialize_net(args.config_path, load_model=True, save_path=args.save_path)
    dataset = ContactDataset(args.data_path, config['data'])
    panda_pb = PandaPB(contactnet, dataset, config)
    successful = panda_pb.pb_execute(args)
