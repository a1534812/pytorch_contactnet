import os
import os.path
import sys
import argparse
import numpy as np
import math
import time

import torch
from model.contactnet import ContactNet
import model.utils.config_utils as config_utils
from data_utils import compute_labels
from dataset import get_dataloader
from torch.utils.tensorboard import SummaryWriter
import copy
from torch_geometric.nn import fps

def initialize_loaders(data_pth, data_config, include_val=False):
    train_loader = get_dataloader(data_pth, data_config)
    if include_val:
        val_loader = get_dataloader(data_pth, data_config)
    else:
        val_loader = None
    return train_loader, val_loader

def initialize_net(config_file):
    torch.cuda.empty_cache()
    # Read in config yaml file to create config dictionary
    config_dict = config_utils.load_config(config_file)
    #print(config_dict)
    # Init net
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    contactnet = ContactNet(config_dict, device).to(device)
    return contactnet, config_dict

def train(model, config, train_loader, val_loader=None, epochs=1, save=True, save_pth=None, args=None):
    optimizer = torch.optim.Adam(model.parameters(), lr=config['train']['lr']) #, weight_decay=0.1)
    #scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=40)
    writer = SummaryWriter()
    torch.autograd.set_detect_anomaly(True)
    for epoch in range(epochs):
        # Train
        model.train()
        running_loss = 0.0
        for i, data in enumerate(train_loader):
            scene_pcds, normals, cam_poses, gt_dict = data
            # scene_pcds shape is (batch size, num points, 3)
            data_shape = scene_pcds.shape
            batch_list = torch.arange(0, data_shape[0])
            batch_list = batch_list[:, None].repeat(1, data_shape[1])
            batch_list = batch_list.view(-1).long().to(model.device)
            
            pcd = scene_pcds.view(-1, data_shape[2]).to(model.device)
            expanded_pcd = copy.deepcopy(pcd.detach().cpu())

            with torch.no_grad():
                idx = fps(expanded_pcd[:, :3], batch_list.detach().cpu(), 2048/20000) # TODO: take out hard coded ratio
                expanded_pcd = expanded_pcd[idx]
                expanded_pcd = expanded_pcd.view(data_shape[0], -1, 3)
                grasp_poses = gt_dict['grasp_poses'] #currently in the wrong shape, need to expand and rebatch for label computation
                grasp_poses = grasp_poses[0].view(data_shape[0], -1, 4, 4) # B x num_label_points x 4 x 4

                # farthest point sample the pointcloud

                gt_points = gt_dict['contact_pts']
                pcd_shape_batched = (gt_points.shape[0], gt_points.shape[2]//gt_points.shape[0], -1)
                gt_points = gt_points[0].view(pcd_shape_batched) #.to(model.device)
                grasp_poses, success_idxs, base_dirs, width, success, approach_dirs = compute_labels(gt_points,
                                                                                        expanded_pcd[:, :, :3],
                                                                                        cam_poses,
                                                                                        gt_dict['base_dirs'],
                                                                                        gt_dict['approach_dirs'],
                                                                                        gt_dict['offsets'],
                                                                                        grasp_poses,
                                                                                        config['data'])
                
                labels_dict = {}
                labels_dict['success_idxs'] = success_idxs
                labels_dict['success'] = success
                labels_dict['grasps'] = grasp_poses
                labels_dict['width'] = width

            optimizer.zero_grad()
            
            #start_time = time.time()
            points, pred_grasps, pred_successes, pred_widths = model(pcd[:, 3:], pos=pcd[:, :3], batch=batch_list, idx=idx, k=None)
            #end_time = time.time()
            #print('Delta time: ', end_time - start_time)
            np.save('first_pcd', points[1].detach().cpu())
            #print(pred_successes[1].shape)
            np.save('success_tensor', pred_successes.detach().cpu()[1])
            loss_list = model.loss(pred_grasps, pred_successes, pred_widths, labels_dict, args)
            loss = loss_list[-1]
            writer.add_scalar('Loss/total', loss, i)
            
            loss.backward() #retain_graph=True)
            #print(model.multihead[3][3].weight.grad[0, :8])
            optimizer.step()
            #scheduler.step()
            running_loss += loss
            
            if i%10 == 0:
                print('[Epoch: %d, Batch: %4d / %4d], Train Loss: %.3f' % (epoch + 1, (i) + 1, len(train_loader), running_loss/10))
                #print('CONF', loss_list[0].item(), 'ADD-S', loss_list[1].item(), 'WIDTH', loss_list[2].item())
                #print(pred_successes)
                running_loss = 0.0

        # Validation
        model.eval()
        if val_loader:
            with torch.no_grad():
                for i, data in enumerate(val_loader):
                    scene_pcds, label_dicts  = data
                    points, pred_grasps, pred_successes, pred_widths = model(scene_pcds)
                    val_loss = model.loss(pred_grasps, pred_successes, pred_widths, label_dicts)
            print('Validation Loss: %.3f %%' % val_loss)

        # save the model
        if save:
            torch.save(model.state_dict(), save_pth)

if __name__=='__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('--epochs', type=int, default=1, help='number of epochs to run')
    parser.add_argument('--save_data', type=bool, default=True, help='whether or not to save data (save to path with arg --save_path)')
    parser.add_argument('--config_path', type=str, default='./model/', help='path to config yaml file')
    parser.add_argument('--save_path', type=str, default='./checkpoints/model_save.pth', help='path to save file for main net')
    parser.add_argument('--data_path', type=str, default='/home/alinasar/acronym/scene_contacts', help='path to acronym dataset with Contact-GraspNet folder')
    parser.add_argument('--root_path', type=str, default='/home/alinasar/pytorch_contactnet/', help='root path to repo')
    args = parser.parse_args()

    # initialize dataloaders
    #train_loader, val_loader = initialize_loaders(args.data_path)
    
    contactnet, config= initialize_net(args.config_path)
    data_config = config['data']
    train_loader, val_loader = initialize_loaders(args.data_path, data_config)

    train(contactnet, config, train_loader, val_loader, args.epochs, args.save_data, args.save_path, args)
