import torch
import math
import torch.distributed as dist
import pdb

from torch.nn import functional as F
from utils.comm import get_world_size

from model.anno_encoder import Anno_Encoder
from model.layers.utils import select_point_of_interest
from model.utils import Uncertainty_Reg_Loss, Laplace_Loss

from model.layers.focal_loss import *
from model.layers.iou_loss import *
from model.head.depth_losses import *
from model.layers.utils import Converter_key2channel

def get_rotys_from_locs(locs):

	th_k1k2 = torch.atan(  (locs[:,0,2] - locs[:,1,2])  /  (locs[:,0,0] - locs[:,1,0])   )
	th_k4k3 = torch.atan(  (locs[:,3,2] - locs[:,2,2])  /  (locs[:,3,0] - locs[:,2,0])   )
	th_k5k6 = torch.atan(  (locs[:,4,2] - locs[:,5,2])  /  (locs[:,4,0] - locs[:,5,0])   )
	th_k8k7 = torch.atan(  (locs[:,7,2] - locs[:,6,2])  /  (locs[:,7,0] - locs[:,6,0])   )

	avg = (th_k1k2 + th_k4k3 + th_k5k6 + th_k8k7) / 4

	return avg

def get_dims_from_locs(locs):

    # locs (B,8,3)
	h_k5k1 = torch.sqrt( (locs[:,4,0] - locs[:,0,0])**2 + (locs[:,4,1] - locs[:,0,1])**2 + (locs[:,4,2] - locs[:,0,2])**2  )
	h_k8k4 = torch.sqrt( (locs[:,7,0] - locs[:,3,0])**2 + (locs[:,7,1] - locs[:,3,1])**2 + (locs[:,7,2] - locs[:,3,2])**2  )
	h_k6k2 = torch.sqrt( (locs[:,5,0] - locs[:,1,0])**2 + (locs[:,5,1] - locs[:,1,1])**2 + (locs[:,5,2] - locs[:,1,2])**2  )
	h_k7k3 = torch.sqrt( (locs[:,6,0] - locs[:,2,0])**2 + (locs[:,6,1] - locs[:,2,1])**2 + (locs[:,6,2] - locs[:,2,2])**2 )

	l_k2k1 = torch.sqrt( (locs[:,1,0] - locs[:,0,0])**2 + (locs[:,1,1] - locs[:,0,1])**2 + (locs[:,1,2] - locs[:,0,2] **2) )
	l_k3k4 = torch.sqrt( (locs[:,2,0] - locs[:,3,0])**2 + (locs[:,2,1] - locs[:,3,1])**2  + (locs[:,2,2] - locs[:,3,2])**2  )
	l_k6k5 = torch.sqrt( (locs[:,5,0] - locs[:,4,0])**2 + (locs[:,5,1] - locs[:,4,1])**2 + (locs[:,5,2] - locs[:,4,2])**2 )
	l_k7k8 = torch.sqrt( (locs[:,6,0] - locs[:,7,0])**2 + (locs[:,6,1] - locs[:,7,1])**2 + (locs[:,6,2] - locs[:,7,2])**2 )

	w_k1k4 = torch.sqrt( (locs[:,0,0] - locs[:,3,0])**2  + (locs[:,0,1] - locs[:,3,1])**2  + (locs[:,0,2] - locs[:,3,2])**2  )
	w_k2k3 = torch.sqrt( (locs[:,1,0] - locs[:,2,0])**2  + (locs[:,1,1] - locs[:,2,1])**2  + (locs[:,1,2] - locs[:,2,2])**2 )
	w_k6k7 = torch.sqrt( (locs[:,5,0] - locs[:,6,0])**2  + (locs[:,5,1] - locs[:,6,1])**2  + (locs[:,5,2] - locs[:,6,2])**2 )
	w_k5k8 = torch.sqrt( (locs[:,4,0] - locs[:,7,0])**2  + (locs[:,4,1] - locs[:,7,1])**2  + (locs[:,4,2] - locs[:,7,2])**2 )
	#print(l_k2k1, l_k3k4, l_k6k5, l_k7k8)
 



	avg_h = (h_k5k1 + h_k8k4 + h_k6k2 + h_k7k3) / 4
	avg_l = (l_k2k1 + l_k3k4 + l_k6k5 + l_k7k8) / 4
	avg_w = (w_k1k4 + w_k2k3 + w_k6k7 + w_k5k8) / 4
	result = torch.stack((avg_h, avg_w, avg_l), dim=1)

	return result


def make_loss_evaluator(cfg):
	loss_evaluator = Loss_Computation(cfg=cfg)
	return loss_evaluator

class Loss_Computation():
	def __init__(self, cfg):
		
		self.anno_encoder = Anno_Encoder(cfg)
		self.key2channel = Converter_key2channel(keys=cfg.MODEL.HEAD.REGRESSION_HEADS, channels=cfg.MODEL.HEAD.REGRESSION_CHANNELS)
		
		self.max_objs = cfg.DATASETS.MAX_OBJECTS
		self.center_sample = cfg.MODEL.HEAD.CENTER_SAMPLE
		self.regress_area = cfg.MODEL.HEAD.REGRESSION_AREA
		self.heatmap_type = cfg.MODEL.HEAD.HEATMAP_TYPE
		self.corner_depth_sp = cfg.MODEL.HEAD.SUPERVISE_CORNER_DEPTH
		self.loss_keys = cfg.MODEL.HEAD.LOSS_NAMES

		self.world_size = get_world_size()
		self.dim_weight = torch.as_tensor(cfg.MODEL.HEAD.DIMENSION_WEIGHT).view(1, 3)
		self.uncertainty_range = cfg.MODEL.HEAD.UNCERTAINTY_RANGE

		# loss functions
		loss_types = cfg.MODEL.HEAD.LOSS_TYPE
		self.cls_loss_fnc = FocalLoss(cfg.MODEL.HEAD.LOSS_PENALTY_ALPHA, cfg.MODEL.HEAD.LOSS_BETA) # penalty-reduced focal loss
		self.iou_loss = IOULoss(loss_type=loss_types[2]) # iou loss for 2D detection

		# depth loss
		if loss_types[3] == 'berhu': self.depth_loss = Berhu_Loss()
		elif loss_types[3] == 'inv_sig': self.depth_loss = Inverse_Sigmoid_Loss()
		elif loss_types[3] == 'log': self.depth_loss = Log_L1_Loss()
		elif loss_types[3] == 'L1': self.depth_loss = F.l1_loss
		else: raise ValueError

		# regular regression loss
		self.reg_loss = loss_types[1]
		self.reg_loss_fnc = F.smooth_l1_loss if loss_types[1] == 'L1' else F.smooth_l1_loss
		self.keypoint_loss_fnc = F.l1_loss

		# multi-bin loss setting for orientation estimation
		self.multibin = (cfg.INPUT.ORIENTATION == 'multi-bin')
		self.orien_bin_size = cfg.INPUT.ORIENTATION_BIN_SIZE
		self.trunc_offset_loss_type = cfg.MODEL.HEAD.TRUNCATION_OFFSET_LOSS

		self.loss_weights = {}
		for key, weight in zip(cfg.MODEL.HEAD.LOSS_NAMES, cfg.MODEL.HEAD.INIT_LOSS_WEIGHT): self.loss_weights[key] = weight

		# whether to compute corner loss
		self.compute_direct_depth_loss = 'depth_loss' in self.loss_keys
		self.compute_keypoint_depth_loss = 'keypoint_depth_loss' in self.loss_keys
		self.compute_weighted_depth_loss = 'weighted_avg_depth_loss' in self.loss_keys
		self.compute_corner_loss = 'corner_loss' in self.loss_keys
		self.separate_trunc_offset = 'trunc_offset_loss' in self.loss_keys
		
		self.pred_direct_depth = 'depth' in self.key2channel.keys
		self.depth_with_uncertainty = 'depth_uncertainty' in self.key2channel.keys
		self.compute_keypoint_corner = 'corner_offset' in self.key2channel.keys
		self.corner_with_uncertainty = 'corner_uncertainty' in self.key2channel.keys

		self.uncertainty_weight = cfg.MODEL.HEAD.UNCERTAINTY_WEIGHT # 1.0
		self.keypoint_xy_weights = cfg.MODEL.HEAD.KEYPOINT_XY_WEIGHT # [1, 1]
		self.keypoint_norm_factor = cfg.MODEL.HEAD.KEYPOINT_NORM_FACTOR # 1.0
		self.modify_invalid_keypoint_depths = cfg.MODEL.HEAD.MODIFY_INVALID_KEYPOINT_DEPTH

		# depth used to compute 8 corners
		self.corner_loss_depth = cfg.MODEL.HEAD.CORNER_LOSS_DEPTH
		self.eps = 1e-5

	def prepare_targets(self, targets):
		# clses
		heatmaps = torch.stack([t.get_field("hm") for t in targets])
		cls_ids = torch.stack([t.get_field("cls_ids") for t in targets])
		offset_3D = torch.stack([t.get_field("offset_3D") for t in targets])
		# 2d detection
		target_centers = torch.stack([t.get_field("target_centers") for t in targets])
		bboxes = torch.stack([t.get_field("2d_bboxes") for t in targets])
		# 3d detection
		keypoints = torch.stack([t.get_field("keypoints") for t in targets])
		keypoints_depth_mask = torch.stack([t.get_field("keypoints_depth_mask") for t in targets])
		dimensions = torch.stack([t.get_field("dimensions") for t in targets])
		locations = torch.stack([t.get_field("locations") for t in targets])
		rotys = torch.stack([t.get_field("rotys") for t in targets])
		alphas = torch.stack([t.get_field("alphas") for t in targets])
		orientations = torch.stack([t.get_field("orientations") for t in targets])
		# utils
		pad_size = torch.stack([t.get_field("pad_size") for t in targets])
		calibs = [t.get_field("calib") for t in targets]
		reg_mask = torch.stack([t.get_field("reg_mask") for t in targets])
		reg_weight = torch.stack([t.get_field("reg_weight") for t in targets])
		ori_imgs = torch.stack([t.get_field("ori_img") for t in targets])
		trunc_mask = torch.stack([t.get_field("trunc_mask") for t in targets])
		keypointsadd = torch.stack([t.get_field("keypointsadd") for t in targets])
		keypointsours = keypointsadd[:, :, 0:42].clone()
        
		#print(locations.shape, keypointsadd.shape) #for ind in range(locations.shape[0])
        
        
		for i in range(locations.shape[0]):
			ind = 0
			for j in range(89):
				keypointsadd[i, :, ind] = keypointsadd[i, :, ind] + locations[i, :, 0]
				ind = ind + 1
				keypointsadd[i, :, ind] = keypointsadd[i, :, ind] + locations[i, :, 1]
				ind = ind + 1
				keypointsadd[i, :, ind] = keypointsadd[i, :, ind] + locations[i, :, 2]
				ind = ind + 1
        
		for i in range(locations.shape[0]):
			keypointsours[i, :, 0] = locations[i, :, 0] + (dimensions[i, : , 1]/2 - 0.1)
			keypointsours[i, :, 1] = locations[i, :, 1]
			keypointsours[i, :, 2] = locations[i, :, 2]
			keypointsours[i, :, 3] = locations[i, :, 0] - (dimensions[i, : , 1]/2 + 0.1)
			keypointsours[i, :, 4] = locations[i, :, 1]
			keypointsours[i, :, 5] = locations[i, :, 2]
			keypointsours[i, :, 6] = locations[i, :, 0]
			keypointsours[i, :, 7] = locations[i, :, 1] + (dimensions[i, : , 0]/2 - 0.1)
			keypointsours[i, :, 8] = locations[i, :, 2]
			keypointsours[i, :, 9] = locations[i, :, 0]
			keypointsours[i, :, 10] = locations[i, :, 1] - (dimensions[i, : , 0]/2 + 0.1)
			keypointsours[i, :, 11] = locations[i, :, 2]
			keypointsours[i, :, 12] = locations[i, :, 0] 
			keypointsours[i, :, 13] = locations[i, :, 1]
			keypointsours[i, :, 14] = locations[i, :, 2] + (dimensions[i, : , 2]/2 - 0.01)
			keypointsours[i, :, 15] = locations[i, :, 0] 
			keypointsours[i, :, 16] = locations[i, :, 1]
			keypointsours[i, :, 17] = locations[i, :, 2] - (dimensions[i, : , 2]/2 + 0.01)
            
			keypointsours[i, :, 18] = locations[i, :, 0] + (dimensions[i, : , 1]/4 - 0.1)
			keypointsours[i, :, 19] = locations[i, :, 1] + (dimensions[i, : , 0]/4 - 0.1)
			keypointsours[i, :, 20] = locations[i, :, 2] + (dimensions[i, : , 2]/4 - 0.01)
            
			keypointsours[i, :, 21] = locations[i, :, 0] - (dimensions[i, : , 1]/4 + 0.1)
			keypointsours[i, :, 22] = locations[i, :, 1] - (dimensions[i, : , 0]/4 + 0.1)
			keypointsours[i, :, 23] = locations[i, :, 2] - (dimensions[i, : , 2]/4 + 0.01)
            
			keypointsours[i, :, 24] = locations[i, :, 0] + (dimensions[i, : , 1]/4 - 0.1)
			keypointsours[i, :, 25] = locations[i, :, 1] - (dimensions[i, : , 0]/4 + 0.1)
			keypointsours[i, :, 26] = locations[i, :, 2] - (dimensions[i, : , 2]/4 + 0.01)
            
			keypointsours[i, :, 27] = locations[i, :, 0] + (dimensions[i, : , 1]/4 - 0.1)
			keypointsours[i, :, 28] = locations[i, :, 1] + (dimensions[i, : , 0]/4 - 0.1)
			keypointsours[i, :, 29] = locations[i, :, 2] - (dimensions[i, : , 2]/4 + 0.01)
            
			keypointsours[i, :, 30] = locations[i, :, 0] - (dimensions[i, : , 1]/4 + 0.1)
			keypointsours[i, :, 31] = locations[i, :, 1] + (dimensions[i, : , 0]/4 - 0.1)
			keypointsours[i, :, 32] = locations[i, :, 2] - (dimensions[i, : , 2]/4 + 0.01)
            
			keypointsours[i, :, 33] = locations[i, :, 0] - (dimensions[i, : , 1]/4 + 0.1)
			keypointsours[i, :, 34] = locations[i, :, 1] - (dimensions[i, : , 0]/4 + 0.1)
			keypointsours[i, :, 35] = locations[i, :, 2] + (dimensions[i, : , 2]/4 - 0.01)
            
			keypointsours[i, :, 36] = locations[i, :, 0] +(dimensions[i, : , 1]/4 - 0.1)
			keypointsours[i, :, 37] = locations[i, :, 1] - (dimensions[i, : , 0]/4 + 0.1)
			keypointsours[i, :, 38] = locations[i, :, 2] + (dimensions[i, : , 2]/4 - 0.01)
            
			keypointsours[i, :, 39] = locations[i, :, 0] - (dimensions[i, : , 1]/4 + 0.1)
			keypointsours[i, :, 40] = locations[i, :, 1] + (dimensions[i, : , 0]/4 - 0.1)
			keypointsours[i, :, 41] = locations[i, :, 2] + (dimensions[i, : , 2]/4 - 0.01)


		return_dict = dict(cls_ids=cls_ids, target_centers=target_centers, bboxes=bboxes, keypoints=keypoints, dimensions=dimensions,
			locations=locations, rotys=rotys, alphas=alphas, calib=calibs, pad_size=pad_size, reg_mask=reg_mask, reg_weight=reg_weight,
			offset_3D=offset_3D, ori_imgs=ori_imgs, trunc_mask=trunc_mask, orientations=orientations, keypoints_depth_mask=keypoints_depth_mask, keypointsadd = keypointsadd, keypointsours = keypointsours,
		)

		return heatmaps, return_dict

	def prepare_predictions(self, targets_variables, predictions):
		pred_regression = predictions['reg']
		batch, channel, feat_h, feat_w = pred_regression.shape

		# 1. get the representative points
		targets_bbox_points = targets_variables["target_centers"] # representative points

		reg_mask_gt = targets_variables["reg_mask"]
		flatten_reg_mask_gt = reg_mask_gt.view(-1).bool()

		# the corresponding image_index for each object, used for finding pad_size, calib and so on
		batch_idxs = torch.arange(batch).view(-1, 1).expand_as(reg_mask_gt).reshape(-1)
		batch_idxs = batch_idxs[flatten_reg_mask_gt].to(reg_mask_gt.device) 

		valid_targets_bbox_points = targets_bbox_points.view(-1, 2)[flatten_reg_mask_gt]

		# fcos-style targets for 2D
		target_bboxes_2D = targets_variables['bboxes'].view(-1, 4)[flatten_reg_mask_gt]
		target_bboxes_height = target_bboxes_2D[:, 3] - target_bboxes_2D[:, 1]
		target_bboxes_width = target_bboxes_2D[:, 2] - target_bboxes_2D[:, 0]

		target_regression_2D = torch.cat((valid_targets_bbox_points - target_bboxes_2D[:, :2], target_bboxes_2D[:, 2:] - valid_targets_bbox_points), dim=1)
		mask_regression_2D = (target_bboxes_height > 0) & (target_bboxes_width > 0)
		target_regression_2D = target_regression_2D[mask_regression_2D]

		# targets for 3D
		target_clses = targets_variables["cls_ids"].view(-1)[flatten_reg_mask_gt]
		target_depths_3D = targets_variables['locations'][..., -1].view(-1)[flatten_reg_mask_gt]
		target_rotys_3D = targets_variables['rotys'].view(-1)[flatten_reg_mask_gt]
		target_alphas_3D = targets_variables['alphas'].view(-1)[flatten_reg_mask_gt]
		target_offset_3D = targets_variables["offset_3D"].view(-1, 2)[flatten_reg_mask_gt]
		target_dimensions_3D = targets_variables['dimensions'].view(-1, 3)[flatten_reg_mask_gt]
		target_kp_3D = targets_variables['keypointsadd'].view(-1, 267)[flatten_reg_mask_gt]

		
		target_orientation_3D = targets_variables['orientations'].view(-1, targets_variables['orientations'].shape[-1])[flatten_reg_mask_gt]
		target_locations_3D = self.anno_encoder.decode_location_flatten(valid_targets_bbox_points, target_offset_3D, target_depths_3D, 
										targets_variables['calib'], targets_variables['pad_size'], batch_idxs)

		target_corners_3D = self.anno_encoder.encode_box3d(target_rotys_3D, target_dimensions_3D, target_locations_3D)
		target_bboxes_3D = torch.cat((target_locations_3D, target_dimensions_3D, target_rotys_3D[:, None]), dim=1)

		target_trunc_mask = targets_variables['trunc_mask'].view(-1)[flatten_reg_mask_gt]
		obj_weights = targets_variables["reg_weight"].view(-1)[flatten_reg_mask_gt]

		# 2. extract corresponding predictions
		pred_regression_pois_3D = select_point_of_interest(batch, targets_bbox_points, pred_regression).view(-1, channel)[flatten_reg_mask_gt]
		
		pred_regression_2D = F.relu(pred_regression_pois_3D[mask_regression_2D, self.key2channel('2d_dim')])
		pred_offset_3D = pred_regression_pois_3D[:, self.key2channel('3d_offset')]
		pred_dimensions_offsets_3D = pred_regression_pois_3D[:, self.key2channel('3d_dim')]
		pred_orientation_3D = torch.cat((pred_regression_pois_3D[:, self.key2channel('ori_cls')], 
									pred_regression_pois_3D[:, self.key2channel('ori_offset')]), dim=1)
		pred_keypointsadd = pred_regression_pois_3D[:, self.key2channel('add_keypoints')]
		#pred_keypointsadd_depth = pred_keypointsadd[:, 0].clone()
        
		#for i in range(pred_keypointsadd.shape[0]):
			#pred_keypointsadd_depth[i] = (pred_keypointsadd[i, 2] + pred_keypointsadd[i, 5] + pred_keypointsadd[i, 8] + pred_keypointsadd[i, 11] + pred_keypointsadd[i, 14] + pred_keypointsadd[i, 17] +  pred_keypointsadd[i, 20] + pred_keypointsadd[i, 23] + pred_keypointsadd[i, 26] + pred_keypointsadd[i, 29] + pred_keypointsadd[i, 32] + pred_keypointsadd[i, 35] + pred_keypointsadd[i, 38] + pred_keypointsadd[i, 41])/14
		pred_keypointsadd_depth = pred_regression_pois_3D[:, -1]
		#print(pred_keypointsadd, target_kp_3D)        
        
		
		# decode the pred residual dimensions to real dimensions
		pred_dimensions_3D = self.anno_encoder.decode_dimension(target_clses, pred_dimensions_offsets_3D)

		# preparing outputs
		targets = { 'reg_2D': target_regression_2D, 'offset_3D': target_offset_3D, 'depth_3D': target_depths_3D, 'orien_3D': target_orientation_3D,
					'dims_3D': target_dimensions_3D, 'corners_3D': target_corners_3D, 'width_2D': target_bboxes_width, 'rotys_3D': target_rotys_3D,
					'cat_3D': target_bboxes_3D, 'trunc_mask_3D': target_trunc_mask, 'height_2D': target_bboxes_height, 'keypointsadd': target_kp_3D,
				}

		preds = {'reg_2D': pred_regression_2D, 'offset_3D': pred_offset_3D, 'orien_3D': pred_orientation_3D, 'dims_3D': pred_dimensions_3D, 'pred_keypointsadd_depth': pred_keypointsadd_depth, 'pred_keypointsadd': pred_keypointsadd}
		reg_nums = {'reg_2D': mask_regression_2D.sum(), 'reg_3D': flatten_reg_mask_gt.sum(), 'reg_obj': flatten_reg_mask_gt.sum()}
		weights = {'object_weights': obj_weights}

		# predict the depth with direct regression
		if self.pred_direct_depth:
			pred_depths_offset_3D = pred_regression_pois_3D[:, self.key2channel('depth')].squeeze(-1)
			pred_direct_depths_3D = self.anno_encoder.decode_depth(pred_depths_offset_3D)
                #####################################################################################################################################################################################
			preds['depth_3D'] = pred_direct_depths_3D

		# predict the uncertainty of depth regression
		if self.depth_with_uncertainty:
                #####################################################################################################################################################################################
			preds['depth_uncertainty'] = pred_regression_pois_3D[:, self.key2channel('depth_uncertainty')].squeeze(-1)
			if self.uncertainty_range is not None:
				preds['depth_uncertainty'] = torch.clamp(preds['depth_uncertainty'], min=self.uncertainty_range[0], max=self.uncertainty_range[1])

			# else:
			# 	print('depth_uncertainty: {:.2f} +/- {:.2f}'.format(
			# 		preds['depth_uncertainty'].mean().item(), preds['depth_uncertainty'].std().item()))

		# predict the keypoints
		if self.compute_keypoint_corner:
			# targets for keypoints
			target_corner_keypoints = targets_variables["keypoints"].view(flatten_reg_mask_gt.shape[0], -1, 3)[flatten_reg_mask_gt]
			targets['keypoints'] = target_corner_keypoints[..., :2]
			targets['keypoints_mask'] = target_corner_keypoints[..., -1]
			reg_nums['keypoints'] = targets['keypoints_mask'].sum()

			# mask for whether depth should be computed from certain group of keypoints
			target_corner_depth_mask = targets_variables["keypoints_depth_mask"].view(-1, 3)[flatten_reg_mask_gt]
			targets['keypoints_depth_mask'] = target_corner_depth_mask

			# predictions for keypoints
			pred_keypoints_3D = pred_regression_pois_3D[:, self.key2channel('corner_offset')]
			pred_keypoints_3D = pred_keypoints_3D.view(flatten_reg_mask_gt.sum(), -1, 3)#;print(pred_keypoints_3D.shape)
                        #pred_keypoints_depths_3D = torch.tenso


			#pred_keypoints_depths_3D = self.anno_encoder.decode_depth_from_keypoints_batch(pred_keypoints_3D, pred_dimensions_3D,
		        #										targets_variables['calib'], batch_idxs);print(pred_keypoints_depths_3D.shape)

			pred_keypoints_depths_3D = torch.squeeze(pred_keypoints_3D[:,0,0:2].clone())

			for i in range(pred_keypoints_3D.shape[0]):
				pred_keypoints_depths_3D[i,0] = ((pred_keypoints_3D[i,0,2] + pred_keypoints_3D[i,4,2])/2 + (pred_keypoints_3D[i,2,2] + pred_keypoints_3D[i,6,2])/2)/2
				pred_keypoints_depths_3D[i,1] = ((pred_keypoints_3D[i,3,2] + pred_keypoints_3D[i,7,2])/2 + (pred_keypoints_3D[i,1,2] + pred_keypoints_3D[i,5,2])/2)/2

                        #pred_keypoints_3D
                        #pred_keypoints_depths_3D = 
                        #print(pred_keypoints_depths_3D.shape)

			preds['keypoints'] = pred_keypoints_3D#; #print("*"*3, pred_keypoints_depths_3D.shape) 
			preds['keypoints_depths'] = pred_keypoints_depths_3D
			pred_dimensions_3D = get_dims_from_locs(pred_keypoints_3D)
			#print(pred_keypoints_3D)
            
		preds['dims_3D'] = pred_dimensions_3D
		#print(preds['dims_3D'])


		# predict the uncertainties of the solved depths from groups of keypoints
		if self.corner_with_uncertainty:
			preds['corner_offset_uncertainty'] = pred_regression_pois_3D[:, self.key2channel('corner_uncertainty')]
			preds['add_keypoints_uncertainty'] = pred_regression_pois_3D[:, self.key2channel('add_keypoints_uncertainty')]

			if self.uncertainty_range is not None:
				preds['corner_offset_uncertainty'] = torch.clamp(preds['corner_offset_uncertainty'], min=self.uncertainty_range[0], max=self.uncertainty_range[1])
				preds['add_keypoints_uncertainty'] = torch.clamp(preds['add_keypoints_uncertainty'], min=self.uncertainty_range[0], max=self.uncertainty_range[1])
        

			# else:
			# 	print('keypoint depth uncertainty: {:.2f} +/- {:.2f}'.format(
			# 		preds['corner_offset_uncertainty'].mean().item(), preds['corner_offset_uncertainty'].std().item()))

		# compute the corners of the predicted 3D bounding boxes for the corner loss
		if self.corner_loss_depth == 'direct':
			pred_corner_depth_3D = pred_direct_depths_3D

		elif self.corner_loss_depth == 'keypoint_mean':
			pred_corner_depth_3D = preds['keypoints_depths'].mean(dim=1)
		
		else:
			assert self.corner_loss_depth in ['soft_combine', 'hard_combine']
			# make sure all depths and their uncertainties are predicted
			pred_combined_uncertainty = torch.cat((preds['depth_uncertainty'].unsqueeze(-1), preds['corner_offset_uncertainty'], preds['add_keypoints_uncertainty']), dim=1).exp()         
			pred_combined_depths = torch.cat((pred_direct_depths_3D.unsqueeze(-1), preds['keypoints_depths'], preds['pred_keypointsadd_depth'].unsqueeze(-1)) ,dim=1)
			
			if self.corner_loss_depth == 'soft_combine':
				pred_uncertainty_weights = 1 / pred_combined_uncertainty
				pred_uncertainty_weights = pred_uncertainty_weights / pred_uncertainty_weights.sum(dim=1, keepdim=True)#;print(pred_combined_depths.shape), print(pred_uncertainty_weights.shape)
				pred_corner_depth_3D = torch.sum(pred_combined_depths * pred_uncertainty_weights, dim=1)
				preds['weighted_depths'] = pred_corner_depth_3D
			
			elif self.corner_loss_depth == 'hard_combine':
				pred_corner_depth_3D = pred_combined_depths[torch.arange(pred_combined_depths.shape[0]), pred_combined_uncertainty.argmin(dim=1)]

		# compute the corners
		pred_locations_3D =  torch.squeeze(torch.mean(pred_keypoints_3D, dim = 1))#; print(pred_orientation_3D.shape) #self.anno_encoder.decode_location_flatten(valid_targets_bbox_points, pred_offset_3D, pred_corner_depth_3D, 
										#targets_variables['calib'], targets_variables['pad_size'], batch_idxs)
		# decode rotys and alphas
		#pred_rotys_3D, _ = self.anno_encoder.decode_axes_orientation(pred_orientation_3D, pred_locations_3D)
		# encode corners
		pred_rotys_3D = get_rotys_from_locs(pred_keypoints_3D) #print("*"*10, "Rotys", pred_rotys_3D.shape)
                

		pred_corners_3D = pred_keypoints_3D#; print(pred_locations_3D.shape, pred_dimensions_3D.shape)#self.anno_encoder.encode_box3d(pred_rotys_3D, pred_dimensions_3D, pred_locations_3D);print("*"*10, "corners", pred_corners_3D.shape)
		# concatenate all predictions
		pred_bboxes_3D = torch.cat((pred_locations_3D, pred_dimensions_3D, pred_rotys_3D[:, None]), dim=1)

		preds.update({'corners_3D': pred_corners_3D, 'rotys_3D': pred_rotys_3D, 'cat_3D': pred_bboxes_3D})

		return targets, preds, reg_nums, weights

	def __call__(self, predictions, targets):
		targets_heatmap, targets_variables = self.prepare_targets(targets)

		pred_heatmap = predictions['cls']
		pred_targets, preds, reg_nums, weights = self.prepare_predictions(targets_variables, predictions)

		# heatmap loss
		if self.heatmap_type == 'centernet':
			hm_loss, num_hm_pos = self.cls_loss_fnc(pred_heatmap, targets_heatmap)
			hm_loss = self.loss_weights['hm_loss'] * hm_loss / torch.clamp(num_hm_pos, 1)

		else: raise ValueError

		# synthesize normal factors
		num_reg_2D = reg_nums['reg_2D']
		num_reg_3D = reg_nums['reg_3D']
		num_reg_obj = reg_nums['reg_obj']
		
		trunc_mask = pred_targets['trunc_mask_3D'].bool()
		num_trunc = trunc_mask.sum()
		num_nontrunc = num_reg_obj - num_trunc

		# IoU loss for 2D detection
		#if num_reg_2D > 0:
		#	reg_2D_loss, iou_2D = self.iou_loss(preds['reg_2D'], pred_targets['reg_2D'])
	    	#	reg_2D_loss = self.loss_weights['bbox_loss'] * reg_2D_loss.mean()
		#	iou_2D = iou_2D.mean()
		depth_MAE = (preds['depth_3D'] - pred_targets['depth_3D']).abs() / pred_targets['depth_3D']

		if num_reg_3D > 0:
			# direct depth loss
			if self.compute_direct_depth_loss:
				depth_3D_loss = self.loss_weights['depth_loss'] * self.depth_loss(preds['depth_3D'], pred_targets['depth_3D'])
				real_depth_3D_loss = depth_3D_loss.detach().mean()
				
				if self.depth_with_uncertainty:
					depth_3D_loss = depth_3D_loss * torch.exp(- preds['depth_uncertainty']) + \
							preds['depth_uncertainty'] * self.loss_weights['depth_loss']
				
				depth_3D_loss = depth_3D_loss.mean()
				
			# offset_3D loss
			#offset_3D_loss = self.reg_loss_fnc(preds['offset_3D'], pred_targets['offset_3D']).sum(dim=1)

			# use different loss functions for inside and outside objects
			#if self.separate_trunc_offset:
			#	if self.trunc_offset_loss_type == 'L1':
			#		trunc_offset_loss = offset_3D_loss[trunc_mask]
				
			#	elif self.trunc_offset_loss_type == 'log':
			#		trunc_offset_loss = torch.log(1 + offset_3D_loss[trunc_mask])

			#	trunc_offset_loss = self.loss_weights['trunc_offset_loss'] * trunc_offset_loss.sum() / torch.clamp(trunc_mask.sum(), min=1)
			#	offset_3D_loss = self.loss_weights['offset_loss'] * offset_3D_loss[~trunc_mask].mean()
			#else:
			#	offset_3D_loss = self.loss_weights['offset_loss'] * offset_3D_loss.mean()

			# orientation loss
			#if self.multibin:
			orien_3D_loss = self.loss_weights['orien_loss'] * \
								Real_MultiBin_loss(preds['orien_3D'], pred_targets['orien_3D'], num_bin=self.orien_bin_size)

			# dimension loss
			#dims_3D_loss = self.reg_loss_fnc(preds['dims_3D'], pred_targets['dims_3D'], reduction='none') * self.dim_weight.type_as(preds['dims_3D'])
			#dims_3D_loss = self.loss_weights['dims_loss'] * dims_3D_loss.sum(dim=1).mean()

			pred_IoU_3D = get_iou_3d(preds['corners_3D'], pred_targets['corners_3D']).mean()

			# corner loss
			if self.compute_corner_loss:
				# N x 8 x 3
				IoU_loss_3d = 10*(1 - get_iou_3d(preds['corners_3D'], pred_targets['corners_3D']).mean()) 

				corner_3D_loss = self.loss_weights['corner_loss'] * \
							self.reg_loss_fnc(preds['corners_3D'], pred_targets['corners_3D'], reduction='none').sum(dim=(1,2)).mean()
                
                
				#print(preds['pred_keypointsadd'].shape, pred_targets['keypointsadd'].shape)
				kp_3D_loss = self.loss_weights['corner_loss'] * \
							self.reg_loss_fnc(preds['pred_keypointsadd'], pred_targets['keypointsadd'], reduction='none').sum(dim=(1)).mean()

			if self.compute_keypoint_corner:
				#print("losssss", preds['keypoints'].shape, pred_targets['keypoints'].shape)
				#keypoint_loss = self.loss_weights['keypoint_loss'] * self.keypoint_loss_fnc(preds['keypoints'],
				#				pred_targets['keypoints'], reduction='none').sum(dim=2) * pred_targets['keypoints_mask']
				
				#keypoint_loss = keypoint_loss.sum() / torch.clamp(pred_targets['keypoints_mask'].sum(), min=1)

				if self.compute_keypoint_depth_loss:
					#print(target_keypoints_depth[keypoints_depth_mask].shape)
					pred_targets['keypoints_depth_mask'] = pred_targets['keypoints_depth_mask'][:,1:3]
					pred_keypoints_depth, keypoints_depth_mask = preds['keypoints_depths'], pred_targets['keypoints_depth_mask'].bool()
					target_keypoints_depth = pred_targets['depth_3D'].unsqueeze(-1).repeat(1, 2)
					
					valid_pred_keypoints_depth = pred_keypoints_depth[keypoints_depth_mask]
					invalid_pred_keypoints_depth = pred_keypoints_depth[~keypoints_depth_mask].detach()
					
					# valid and non-valid
					valid_keypoint_depth_loss = self.loss_weights['keypoint_depth_loss'] * self.reg_loss_fnc(valid_pred_keypoints_depth, 
															target_keypoints_depth[keypoints_depth_mask])
					
					invalid_keypoint_depth_loss = self.loss_weights['keypoint_depth_loss'] * self.reg_loss_fnc(invalid_pred_keypoints_depth, 
															target_keypoints_depth[~keypoints_depth_mask])
                    
					pred_targets['add_kp_depth_mask'] = pred_targets['keypoints_depth_mask'][:,0]
					pred_kp_depth, kp_depth_mask = preds['pred_keypointsadd_depth'], pred_targets['add_kp_depth_mask'].bool()
					target_keypoints_depth = pred_targets['depth_3D'].unsqueeze(-1)
					
					valid_pred_kp_depth = pred_kp_depth[kp_depth_mask]
					invalid_pred_kp_depth = pred_kp_depth[~kp_depth_mask].detach()
					
					# valid and non-valid
					valid_add_kp_depth_mask = self.loss_weights['keypoint_depth_loss'] * self.reg_loss_fnc(valid_pred_kp_depth, 
															target_keypoints_depth[kp_depth_mask])
					
					invalid_add_kp_depth_mask = self.loss_weights['keypoint_depth_loss'] * self.reg_loss_fnc(invalid_pred_kp_depth, 
															target_keypoints_depth[~kp_depth_mask])

                    

                    
                    
					
					# for logging
					log_valid_keypoint_depth_loss = valid_keypoint_depth_loss.detach().mean()
                    
					# for logging
					#log_valid_add_kp_depth_mask = valid_add_kp_depth_mask.detach().mean()

					if self.corner_with_uncertainty:
						# center depth, corner 0246 depth, corner 1357 depth
						pred_keypoint_depth_uncertainty = preds['corner_offset_uncertainty']
						#pred_keypointadd_depth_uncertainty = preds['add_keypoints_uncertainty']

						valid_uncertainty = pred_keypoint_depth_uncertainty[keypoints_depth_mask]
						invalid_uncertainty = pred_keypoint_depth_uncertainty[~keypoints_depth_mask]

						valid_keypoint_depth_loss = valid_keypoint_depth_loss * torch.exp(- valid_uncertainty) + \
												self.loss_weights['keypoint_depth_loss'] * valid_uncertainty

						invalid_keypoint_depth_loss = invalid_keypoint_depth_loss * torch.exp(- invalid_uncertainty)

					# average
					valid_keypoint_depth_loss = valid_keypoint_depth_loss.sum() / torch.clamp(keypoints_depth_mask.sum(), 1)
					invalid_keypoint_depth_loss = invalid_keypoint_depth_loss.sum() / torch.clamp((~keypoints_depth_mask).sum(), 1)

					# the gradients of invalid depths are not back-propagated
					if self.modify_invalid_keypoint_depths:
						keypoint_depth_loss = valid_keypoint_depth_loss + invalid_keypoint_depth_loss
					else:
						keypoint_depth_loss = valid_keypoint_depth_loss
                        

					# for logging
					log_valid_add_kp_depth_mask = valid_add_kp_depth_mask.detach().mean()
                    
					# for logging
					#log_valid_add_kp_depth_mask = valid_add_kp_depth_mask.detach().mean()

					if self.corner_with_uncertainty:
						# center depth, corner 0246 depth, corner 1357 depth
						#pred_keypoint_depth_uncertainty = preds['corner_offset_uncertainty']
						pred_keypointadd_depth_uncertainty = preds['add_keypoints_uncertainty']

						valid_uncertainty = pred_keypointadd_depth_uncertainty[kp_depth_mask]
						invalid_uncertainty = pred_keypointadd_depth_uncertainty[~kp_depth_mask]

						valid_add_kp_depth_mask = valid_add_kp_depth_mask * torch.exp(- valid_uncertainty) + \
												self.loss_weights['keypoint_depth_loss'] * valid_uncertainty

						invalid_add_kp_depth_mask = invalid_add_kp_depth_mask * torch.exp(- invalid_uncertainty)

					# average
					valid_add_kp_depth_mask = valid_add_kp_depth_mask.sum() / torch.clamp(kp_depth_mask.sum(), 1)
					invalid_add_kp_depth_mask = invalid_add_kp_depth_mask.sum() / torch.clamp((~kp_depth_mask).sum(), 1)

					# the gradients of invalid depths are not back-propagated
					if self.modify_invalid_keypoint_depths:
						keypoint_depth_loss_add = valid_add_kp_depth_mask + invalid_add_kp_depth_mask
					else:
						keypoint_depth_loss_add = valid_add_kp_depth_mask
                   
                        
                        
                        
				
				# compute the average error for each method of depth estimation
				#print("keypoints_depths", preds['keypoints_depths'].shape)
				keypoint_MAE = (preds['keypoints_depths'] - pred_targets['depth_3D'].unsqueeze(-1)).abs() \
									/ pred_targets['depth_3D'].unsqueeze(-1)
				#print("keypoint_MAE", keypoint_MAE.shape)
				#center_MAE = keypoint_MAE[:, 0].mean()
				keypoint_02_MAE = keypoint_MAE[:, 0].mean()
				keypoint_13_MAE = keypoint_MAE[:, 1].mean()
				#print("pred_keypointsadd_depth", preds['pred_keypointsadd_depth'].shape)
                
				keypoint_MAE_add = (preds['pred_keypointsadd_depth'].unsqueeze(-1) - pred_targets['depth_3D'].unsqueeze(-1)).abs() \
									/ pred_targets['depth_3D'].unsqueeze(-1)
				
				#center_MAE = keypoint_MAE[:, 0].mean()
				keypoint_add_MAE = keypoint_MAE_add.mean()
				#print("keypoint_add_MAE", keypoint_add_MAE.shape)

                
                
                

				if self.corner_with_uncertainty:
					if self.pred_direct_depth and self.depth_with_uncertainty:
						combined_depth = torch.cat((preds['depth_3D'].unsqueeze(1), preds['keypoints_depths'], preds['pred_keypointsadd_depth'].unsqueeze(1)), dim=1)
						combined_uncertainty = torch.cat((preds['depth_uncertainty'].unsqueeze(1), preds['corner_offset_uncertainty'], preds['add_keypoints_uncertainty']), dim=1).exp()
						#print(depth_MAE.unsqueeze(1).shape, keypoint_MAE.shape, keypoint_add_MAE.unsqueeze(1).shape)                        
						combined_MAE = torch.cat((depth_MAE.unsqueeze(1), keypoint_MAE, keypoint_MAE_add), dim=1)
					else:
						combined_depth = preds['keypoints_depths']
						combined_uncertainty = torch.cat((preds['corner_offset_uncertainty'], preds['add_keypoints_uncertainty']), dim = 1).exp()
						combined_MAE = keypoint_MAE

					# the oracle MAE
					lower_MAE = torch.min(combined_MAE, dim=1)[0]
					# the hard ensemble
					hard_MAE = combined_MAE[torch.arange(combined_MAE.shape[0]), combined_uncertainty.argmin(dim=1)]
					# the soft ensemble
					combined_weights = 1 / combined_uncertainty
					combined_weights = combined_weights / combined_weights.sum(dim=1, keepdim=True)
					soft_depths = torch.sum(combined_depth * combined_weights, dim=1)
					soft_MAE = (soft_depths - pred_targets['depth_3D']).abs() / pred_targets['depth_3D']
					# the average ensemble
					mean_depths = combined_depth.mean(dim=1)
					mean_MAE = (mean_depths - pred_targets['depth_3D']).abs() / pred_targets['depth_3D']

					# average
					lower_MAE, hard_MAE, soft_MAE, mean_MAE = lower_MAE.mean(), hard_MAE.mean(), soft_MAE.mean(), mean_MAE.mean()
					#print(dims_3D_loss, orien_3D_loss)
				
					if self.compute_weighted_depth_loss:
						soft_depth_loss = self.loss_weights['weighted_avg_depth_loss'] * \
										self.reg_loss_fnc(soft_depths, pred_targets['depth_3D'], reduction='mean')

			depth_MAE = depth_MAE.mean()

		loss_dict = {
			#'hm_loss':  hm_loss,
			#'bbox_loss': reg_2D_loss,
			#'dims_loss': dims_3D_loss,
			#'orien_loss': orien_3D_loss,
		}
		log_loss_dict = {
			#'2D_IoU': iou_2D.item(),
			'3D_IoU': pred_IoU_3D.item(),
		}

		MAE_dict = {}

		#if self.separate_trunc_offset:
			#loss_dict['offset_loss'] = offset_3D_loss
			#loss_dict['trunc_offset_loss'] = trunc_offset_loss
		#else:
			#loss_dict['offset_loss'] = offset_3D_loss

		if self.compute_corner_loss:
			loss_dict['corner_loss'] = corner_3D_loss
			loss_dict['IoU_loss_3d'] = IoU_loss_3d
			loss_dict['kp_loss'] = kp_3D_loss

		if self.pred_direct_depth:
			loss_dict['depth_loss'] = depth_3D_loss
			log_loss_dict['depth_loss'] = real_depth_3D_loss.item()
			MAE_dict['depth_MAE'] = depth_MAE.item()

		if self.compute_keypoint_corner:
		#	loss_dict['keypoint_loss'] = keypoint_loss

			MAE_dict.update({
				'kp_MAE': keypoint_add_MAE.item(),
				'02_MAE': keypoint_02_MAE.item(),
				'13_MAE': keypoint_13_MAE.item(),
			})

			if self.corner_with_uncertainty:
				MAE_dict.update({
					'lower_MAE': lower_MAE.item(),
					'hard_MAE': hard_MAE.item(),
					'soft_MAE': soft_MAE.item(),
					'mean_MAE': mean_MAE.item(),
				})

		if self.compute_keypoint_depth_loss:
			loss_dict['keypoint_depth_loss'] = keypoint_depth_loss
			log_loss_dict['keypoint_depth_loss'] = log_valid_keypoint_depth_loss.item()
			loss_dict['keypoint_depth_loss_add'] = keypoint_depth_loss_add
			log_loss_dict['keypoint_depth_loss_add'] = log_valid_add_kp_depth_mask.item()
            

		if self.compute_weighted_depth_loss:
			loss_dict['weighted_avg_depth_loss'] = soft_depth_loss

		# loss_dict ===> log_loss_dict
		for key, value in loss_dict.items():
			if key not in log_loss_dict:
				log_loss_dict[key] = value.item()

		# stop when the loss has NaN or Inf
		for v in loss_dict.values():
			if torch.isnan(v).sum() > 0:
				pdb.set_trace()
			if torch.isinf(v).sum() > 0:
				pdb.set_trace()

		log_loss_dict.update(MAE_dict);#print('dims_loss', dims_3D_loss, 'orien_loss', orien_3D_loss, 'offset_loss', offset_3D_loss, 'corner_loss', corner_3D_loss)

		return loss_dict, log_loss_dict

def Real_MultiBin_loss(vector_ori, gt_ori, num_bin=4):
	gt_ori = gt_ori.view(-1, gt_ori.shape[-1]) # bin1 cls, bin1 offset, bin2 cls, bin2 offst

	cls_losses = 0
	reg_losses = 0
	reg_cnt = 0
	for i in range(num_bin):
		# bin cls loss
		cls_ce_loss = F.cross_entropy(vector_ori[:, (i * 2) : (i * 2 + 2)], gt_ori[:, i].long(), reduction='none')
		# regression loss
		valid_mask_i = (gt_ori[:, i] == 1)
		cls_losses += cls_ce_loss.mean()
		if valid_mask_i.sum() > 0:
			s = num_bin * 2 + i * 2
			e = s + 2
			pred_offset = F.normalize(vector_ori[valid_mask_i, s : e])
			reg_loss = F.l1_loss(pred_offset[:, 0], torch.sin(gt_ori[valid_mask_i, num_bin + i]), reduction='none') + \
						F.l1_loss(pred_offset[:, 1], torch.cos(gt_ori[valid_mask_i, num_bin + i]), reduction='none')

			reg_losses += reg_loss.sum()
			reg_cnt += valid_mask_i.sum()

	return cls_losses / num_bin + reg_losses / reg_cnt
