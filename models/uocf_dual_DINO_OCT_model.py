from itertools import chain
from math import e
from sympy import N

import torch
from torch import nn, optim
import torch.nn.functional as F
from .base_model import BaseModel
from . import networks
import os
import time
from .projection import pixel2world
from .projection import ProjectionOCT as Projection
from torchvision.transforms import Normalize
# SlotAttention
from .model_general import MultiDINOStackEncoder
from .transformer_attn import SlotAttentionTransformer, DecoderIPE
from .utils import *
import numpy as np

import torchvision

class uocfDualDINOOCTModel(BaseModel):

	@staticmethod
	def modify_commandline_options(parser, is_train=True):
		"""Add new model-specific options and rewrite default values for existing options.
		Parameters:
			parser -- the option parser
			is_train -- if it is training phase or test phase. You can use this flag to add training-specific or test-specific options.
		Returns:
			the modified parser.
		"""
		parser.add_argument('--num_slots', metavar='K', type=int, default=5, help='Number of supported slots')
		parser.add_argument('--shape_dim', type=int, default=48, help='Dimension of individual z latent per slot')
		parser.add_argument('--color_dim', type=int, default=16, help='Dimension of individual z latent per slot texture')
		parser.add_argument('--attn_iter', type=int, default=3, help='Number of refine iteration in slot attention')
		parser.add_argument('--warmup_steps', type=int, default=1000, help='Warmup steps')
		parser.add_argument('--nss_scale', type=float, default=7, help='Scale of the scene, related to camera matrix')
		parser.add_argument('--render_size', type=int, default=64, help='Shape of patch to render each forward process. Must be Frustum_size/(2^N) where N=0,1,..., Smaller values cost longer time but require less GPU memory.')
		parser.add_argument('--supervision_size', type=int, default=64)
		parser.add_argument('--obj_scale', type=float, default=6, help='slot-centric locality constraint')
		parser.add_argument('--slot_boundary', type=float, default=3., help='slot boundary')
		# parser.add_argument('--world_obj_scale', type=float, default=4.5, help='locality constraint in world space')
		parser.add_argument('--n_freq', type=int, default=5, help='how many increased freq?')
		parser.add_argument('--n_samp', type=int, default=64, help='num of samp per ray')
		parser.add_argument('--n_layer', type=int, default=3, help='num of layers bef/aft skip link in decoder')
		parser.add_argument('--weight_percept', type=float, default=0.006)
		parser.add_argument('--percept_in', type=int, default=100)
		parser.add_argument('--mask_in', type=int, default=0)
		parser.add_argument('--no_locality_epoch', type=int, default=600)
		parser.add_argument('--locality_in', type=int, default=100000)
		parser.add_argument('--locality_out', type=int, default=0)
		parser.add_argument('--bottom', action='store_true', help='one more encoder layer on bottom')
		parser.add_argument('--input_size', type=int, default=64)
		parser.add_argument('--frustum_size', type=int, default=64)
		parser.add_argument('--frustum_size_fine', type=int, default=128) # frustum_size_fine must equal input_size
		parser.add_argument('--attn_decay_steps', type=int, default=2e5)
		parser.add_argument('--freezeInit_ratio', type=float, default=1)
		parser.add_argument('--freezeInit_steps', type=int, default=100000)
		parser.add_argument('--coarse_epoch', type=int, default=600)
		parser.add_argument('--near_plane', type=float, default=6)
		parser.add_argument('--far_plane', type=float, default=20)
		parser.add_argument('--fixed_locality', action='store_true', help='enforce locality in world space instead of transformed view space')
		parser.add_argument('--fg_in_world', action='store_true', help='foreground objects are in world space')
		parser.add_argument('--dens_noise', type=float, default=0., help='Noise added to density may help in mitigating rank collapse')
		parser.add_argument('--invariant_in', type=int, default=0, help='when to start translation invariant decoding')
		parser.add_argument('--sfs_loss', action='store_true', help='use the Slot-Feature-Slot loss')
		parser.add_argument('--weight_sfs', type=float, default=0.1, help='weight of the Slot-Feature-Slot loss')
		parser.add_argument('--position_in', type=int, default=100, help='when to start the position loss')
		parser.add_argument('--weight_position', type=float, default=0.1, help='weight of the position loss')
		parser.add_argument('--feat_dropout_start', type=int, default=100, help='when to start dropout in feature map')
		parser.add_argument('--feat_dropout_min', type=float, default=0, help='dropout rate in feature map')
		parser.add_argument('--feat_dropout_max', type=float, default=1, help='dropout rate in feature map')
		parser.add_argument('--feat_dropout', action='store_true', help='use dropout in feature map')
		parser.add_argument('--all_dropout_ratio', type=float, default=0.25, help='dropout rate in all layers (* shape feat dropout rate)')
		parser.add_argument('--dense_sample_epoch', type=int, default=10000, help='when to start dense sampling')
		parser.add_argument('--n_dense_samp', type=int, default=256, help='number of dense sampling')
		parser.add_argument('--fg_density_loss', action='store_true', help='use density loss for the foreground slot')
		parser.add_argument('--bg_density_loss', action='store_true', help='use density loss for the background slot')
		parser.add_argument('--bg_density_in', type=int, default=0, help='when to start the background density loss')
		parser.add_argument('--bg_penalize_plane', type=int, default=8, help='penalize the background slot if it is too close to the plane')
		parser.add_argument('--weight_bg_density', type=float, default=0.1, help='weight of the background plane penalty')
		parser.add_argument('--frequency_mask', action='store_true', help='use frequency mask in the decoder')
		parser.add_argument('--weight_depth_ranking', type=float, default=1, help='weight of the depth supervision')
		parser.add_argument('--depth_in', type=int, default=1000, help='when to start the depth supervision')
		
		parser.set_defaults(batch_size=1, lr=3e-4, niter_decay=0,
							dataset_mode='multiscenes', niter=1200, custom_lr=True, lr_policy='warmup')

		parser.set_defaults(exp_id='run-{}'.format(time.strftime('%Y-%m-%d-%H-%M-%S')))

		return parser

	def __init__(self, opt):
		"""Initialize this model class.
		Parameters:
			opt -- training/test options
		A few things can be done here.
		- (required) call the initialization function of BaseModel
		- define loss function, visualization images, model names, and optimizers
		"""
		BaseModel.__init__(self, opt)  # call the initialization method of BaseModel
		assert opt.scaled_depth 
		self.loss_names = ['recon', 'perc']
		if self.opt.sfs_loss:
			self.loss_names += ['sfs']
		if self.opt.position_loss:
			self.loss_names += ['pos']
		if self.opt.bg_density_loss:
			self.loss_names += ['bg_density']
		if self.opt.fg_density_loss:
			self.loss_names += ['fg_density']
		if self.opt.depth_supervision:
			self.loss_names	+= ['depth_ranking']
		if self.opt.pseudo_mask_loss:
			self.loss_names += ['pseudo_mask']
		self.set_visual_names()
		self.model_names = ['Encoder', 'SlotAttention', 'Decoder']
		self.perceptual_net = get_perceptual_net().to(self.device)
		self.vgg_norm = Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
		# self.intrinsics = np.loadtxt(os.path.join(opt.dataroot, 'camera_intrinsics_ratio.txt')) if opt.load_intrinsics else None
		render_size = (opt.render_size, opt.render_size)
		frustum_size = [self.opt.frustum_size, self.opt.frustum_size, self.opt.n_samp]
		self.projection = Projection(device=self.device, nss_scale=opt.nss_scale,
									 frustum_size=frustum_size, near=opt.near_plane, far=opt.far_plane, render_size=render_size)
		frustum_size_fine = [self.opt.frustum_size_fine, self.opt.frustum_size_fine, self.opt.n_samp]
		self.projection_fine = Projection(device=self.device, nss_scale=opt.nss_scale,
										  frustum_size=frustum_size_fine, near=opt.near_plane, far=opt.far_plane, render_size=render_size)

		z_dim = opt.color_dim + opt.shape_dim
		self.num_slots = opt.num_slots

		if opt.encoder_type == 'DINO':
			self.pretrained_encoder = torch.hub.load('facebookresearch/dinov2', 'dinov2_vitb14').to(self.device).eval()
			dino_dim = 768
			self.netEncoder = MultiDINOStackEncoder(shape_dim=opt.shape_dim, color_dim=opt.color_dim, input_dim=dino_dim, 
							n_feat_layer=opt.n_feat_layers, global_bg_feature=opt.global_bg_feature)
		else:
			assert False

		self.netSlotAttention = SlotAttentionTransformer(num_slots=opt.num_slots, in_dim=opt.shape_dim+opt.color_dim if opt.color_in_attn else opt.shape_dim, 
					slot_dim=opt.shape_dim+opt.color_dim if opt.color_in_attn else opt.shape_dim, 
					color_dim=0 if opt.color_in_attn else opt.color_dim, momentum=opt.attn_momentum, pos_init=opt.pos_init,
					learnable_pos=not opt.no_learnable_pos, iters=opt.attn_iter, 
					camera_modulation=opt.camera_modulation, camera_dim=16)
							  
		self.netDecoder = DecoderIPE(n_freq=opt.n_freq, input_dim=6*opt.n_freq+3+z_dim, z_dim=z_dim, n_layers=opt.n_layer,
													locality_ratio=opt.obj_scale/opt.nss_scale, fixed_locality=opt.fixed_locality,
													predict_depth_scale = False)
			
		self.netEncoder = self.netEncoder.to(self.device)
		self.netDecoder = self.netDecoder.to(self.device)
		self.netSlotAttention = self.netSlotAttention.to(self.device)

		self.L2_loss = nn.MSELoss()
		self.sfs_loss = SlotFeatureSlotLoss()
		self.pos_loss = PositionSetLoss()

		self.collapse_prevent_iter = opt.collapse_prevent

	def set_visual_names(self, set_depth=False):
		n = self.opt.n_img_each_scene
		n_slot = self.opt.num_slots
		self.visual_names = ['x{}'.format(i) for i in range(n)] + \
							['x_rec{}'.format(i) for i in range(n)] + \
							['slot{}_view{}'.format(k, i) for k in range(n_slot) for i in range(n)] + \
							['unmasked_slot{}_view{}'.format(k, i) for k in range(n_slot) for i in range(n)]
		self.visual_names += ['slot{}_attn'.format(k) for k in range(n_slot)]
		if set_depth:
			self.visual_names += ['disparity_{}'.format(i) for i in range(n)] + \
								 ['disparity_rec{}'.format(i) for i in range(n)]

	def setup(self, opt):
		"""Load and print networks; create schedulers
		Parameters:
			opt (Option class) -- stores all the experiment flags; needs to be a subclass of BaseOptions
		"""
		if self.isTrain:
			if opt.load_pretrain: # load pretraine models, e.g., object NeRF decoder
				assert opt.load_pretrain_path is not None
				unloaded_keys, loaded_keys_frozen, loaded_keys_trainable = self.load_pretrain_networks(opt.load_pretrain_path, opt.load_epoch)
				def get_params(keys, keyword_to_include=None, keyword_to_exclude=None):
					params = [v for k, v in self.netEncoder.named_parameters() if k in keys \
		   						and (keyword_to_include is None or keyword_to_include in k) \
								and (keyword_to_exclude is None or keyword_to_exclude not in k)] + \
							 [v for k, v in self.netSlotAttention.named_parameters() if k in keys \
	 							and (keyword_to_include is None or keyword_to_include in k) \
								and (keyword_to_exclude is None or keyword_to_exclude not in k)] + \
							 [v for k, v in self.netDecoder.named_parameters() if k in keys \
	 							and (keyword_to_include is None or keyword_to_include in k) \
								and (keyword_to_exclude is None or keyword_to_exclude not in k)]
					return params
				
				def get_decoder_params(keys, reverse=False):
					if not reverse:
						params = [v for k, v in self.netDecoder.named_parameters() if k in keys]
					else:
						params = [v for k, v in self.netDecoder.named_parameters() if k not in keys]
					return params

				unloaded_params, loaded_params_frozen, loaded_params_trainable = get_params(unloaded_keys), get_params(loaded_keys_frozen), get_params(loaded_keys_trainable)
				print('Unloaded params:', unloaded_keys, '\n', 'Length:', len(unloaded_keys))
				print('Loaded params (frozen):', loaded_keys_frozen, '\n', 'Length:', len(loaded_keys_frozen))
				print('Loaded params (trainable):', loaded_keys_trainable, '\n', 'Length:', len(loaded_keys_trainable))
				self.optimizers, self.schedulers = [], []
				if len(unloaded_params) > 0:
					self.optimizers.append(optim.Adam(unloaded_params, lr=opt.lr))
					self.schedulers.append(networks.get_scheduler(self.optimizers[-1], opt))
				if len(loaded_params_frozen) > 0:
					self.optimizers.append(optim.Adam(loaded_params_frozen, lr=opt.lr))
					configs = (opt.freezeInit_ratio, opt.freezeInit_steps, 0, opt.attn_decay_steps) # no warmup
					self.schedulers.append(networks.get_freezeInit_scheduler(self.optimizers[-1], params=configs))
				if len(loaded_params_trainable) > 0:
					if self.opt.large_decoder_lr:
						big_lr_params = get_decoder_params(loaded_keys_trainable, reverse=False)
						small_lr_params = get_decoder_params(loaded_keys_trainable, reverse=True)
						self.optimizers.append(optim.Adam([{'params': big_lr_params, 'lr': opt.lr}, {'params': small_lr_params, 'lr': opt.lr/3}]))
					else:
						self.optimizers.append(optim.Adam(loaded_params_trainable, lr=opt.lr))
					configs = (opt.freezeInit_ratio, 0, 0, opt.attn_decay_steps) # no warmup
					self.schedulers.append(networks.get_freezeInit_scheduler(self.optimizers[-1], params=configs))
			else:
				requires_grad = lambda x: x.requires_grad
				params = chain(self.netEncoder.parameters(), self.netSlotAttention.parameters(), self.netDecoder.parameters())
				self.optimizer = optim.Adam(filter(requires_grad, params), lr=opt.lr)
				self.optimizers = [self.optimizer]
				self.schedulers = [networks.get_scheduler(optimizer, opt) for optimizer in self.optimizers]

		if not self.isTrain or opt.continue_train: # resume
			load_suffix = 'iter_{}'.format(opt.load_iter) if opt.load_iter > 0 else opt.epoch
			self.load_networks(load_suffix)
		
		self.print_networks(opt.verbose)

	def set_input(self, input):
		"""Unpack input data from the dataloader and perform necessary pre-processing steps.
		Parameters:
			input: a dictionary that contains the data itself and its metadata information.
		"""
		self.x = input['img_data'].to(self.device) # Nx3xHxW
		self.x_large = input['img_data_large'].to(self.device) if self.opt.encoder_type != 'CNN' else None
		self.cam2world = input['cam2world'].to(self.device)
		if not self.opt.fixed_locality:
			self.cam2world_azi = input['azi_rot'].to(self.device)
		if 'intrinsics' in input:
			self.intrinsics = input['intrinsics'].to(self.device) # overwrite the default intrinsics, n_scenes*3*3
		if input['depth'] is not None:
			self.disparity = input['depth'].to(self.device)

	def encode(self, idx=0, return_global_feature=False):
		"""Encode the input image into a feature map.
		Parameters:
			idx: idx of the image to be encoded (typically 0, if position loss is used, we may use 1)
		Returns:
			feat_shape, feat_color (BxHxWxC), (BxHxWxC)
			class_token (BxC)
		"""
		feature_maps, class_tokens = [], []
		with torch.no_grad(): # B*C*H*W
			outputs = self.pretrained_encoder.get_intermediate_layers(self.x_large[idx:idx+1], n=self.opt.n_feat_layers, reshape=True, return_class_token=True)
		
		for feature_map, class_token in outputs:
			feature_maps.append(feature_map)
			if return_global_feature:
				class_tokens.append(class_token)
			else:
				class_tokens.append(None)

		feature_map_shape, feature_map_color, feature_global = self.netEncoder(feature_maps, feature_maps[-1], class_tokens[-1])  # Bxshape_dimxHxW, Bxcolor_dimxHxW

		feat_shape = feature_map_shape.permute([0, 2, 3, 1]).contiguous()  # BxHxWxC
		feat_color = feature_map_color.permute([0, 2, 3, 1]).contiguous()  # BxHxWxC

		return feat_shape, feat_color, feature_global

	def forward(self, epoch=0):
		"""Run forward pass. This will be called by both functions <optimize_parameters> and <test>."""
		if epoch >= self.opt.depth_in and self.opt.depth_supervision:
			self.set_visual_names(set_depth=True)
		self.weight_percept = self.opt.weight_percept if epoch >= self.opt.percept_in else 0
		dens_noise = self.opt.dens_noise if epoch < self.opt.collapse_prevent else 0
		self.loss_recon, self.loss_perc, self.loss_sfs, self.loss_pos, \
			self.loss_bg_density, self.loss_fg_density, self.loss_depth_ranking, self.loss_pseudo_mask = 0, 0, 0, 0, 0, 0, 0, 0
		dev = self.x[0:1].device
		cam2world_viewer = self.cam2world[0]
		nss2cam0 = self.cam2world[0:1].inverse() if self.opt.fixed_locality else self.cam2world_azi[0:1].inverse()
		if self.opt.fixed_locality: # divide the translation part by self.opt.nss_scale
			nss2cam0 = torch.cat([torch.cat([nss2cam0[:, :3, :3], nss2cam0[:, :3, 3:4]/self.opt.nss_scale], dim=2), 
									nss2cam0[:, 3:4, :]], dim=1) # 1*4*4

		# Encoding images
		feat_shape, feat_color, feat_global = self.encode(0, return_global_feature=self.opt.global_bg_feature)

		# calculate camera cond (R, T, fx, fy, cx, cy) , 1*camera_dim, camera_dim=5, assert camera_normalized
		if self.opt.camera_modulation:
			camExt = torch.cat([cam2world_viewer[:3, :3].flatten(), cam2world_viewer[:3, 3:4].flatten()/self.opt.nss_scale], dim=0)
			camInt = torch.Tensor([self.intrinsics[0, 0, 0], self.intrinsics[0, 1, 1], self.intrinsics[0, 0, 2], self.intrinsics[0, 1, 2]]).to(dev) \
				if self.intrinsics is not None else torch.Tensor([350./320., 350./240., 0., 0.]).to(dev)
			camera_modulation = torch.cat([camExt, camInt], dim=0).unsqueeze(0)
		else:
			camera_modulation = None
	
		# transformer attention
		z_slots, attn, fg_slot_position = self.netSlotAttention(feat_shape, feat_color=feat_color, camera_modulation=camera_modulation)  # 1xKxC, 1xKx2, 1xKxN
		z_slots, fg_slot_position, attn = z_slots.squeeze(0), fg_slot_position.squeeze(0), attn.squeeze(0)  # KxC, Kx2, KxN

		if self.opt.global_bg_feature:
			z_slots[0][-self.opt.color_dim:] = feat_global.squeeze(0) # use the global feature as the background slot
			
		K = attn.shape[0]
			
		cam2world = self.cam2world
		N = cam2world.shape[0]
		if self.opt.stage == 'coarse':
			frustum_size = [self.opt.frustum_size, self.opt.frustum_size, self.opt.n_samp] \
							if epoch < self.opt.dense_sample_epoch \
							else [self.opt.frustum_size, self.opt.frustum_size, self.opt.n_dense_samp]
			(mean, var), z_vals, ray_dir = self.projection.sample_along_rays(cam2world, 
										intrinsics=self.intrinsics,
										frustum_size=frustum_size, stratified=self.opt.stratified if epoch >= self.opt.dense_sample_epoch else False)
			# (NxHxW)xDx3, (NxHxW)xDx3x3, (NxHxW)xD, (NxHxW)x3
			x = F.interpolate(self.x, size=self.opt.supervision_size, mode='bilinear', align_corners=False)
			if self.opt.depth_supervision and epoch >= self.opt.depth_in:
				disparity = F.interpolate(self.disparity, size=self.opt.supervision_size, mode='bilinear', align_corners=False)
			self.z_vals, self.ray_dir = z_vals, ray_dir
		else:
			frustum_size = [self.opt.frustum_size_fine, self.opt.frustum_size_fine, self.opt.n_samp] \
							if epoch < self.opt.dense_sample_epoch \
							else [self.opt.frustum_size_fine, self.opt.frustum_size_fine, self.opt.n_dense_samp]
			W, H, D = self.opt.frustum_size_fine, self.opt.frustum_size_fine, self.opt.n_samp if epoch < self.opt.dense_sample_epoch else self.opt.n_dense_samp
			start_range = self.opt.frustum_size_fine - self.opt.supervision_size
			rs = self.opt.supervision_size # originally render_size
			(mean, var), z_vals, ray_dir = self.projection_fine.sample_along_rays(cam2world, 
										 intrinsics=self.intrinsics,
										 frustum_size=frustum_size, stratified=self.opt.stratified if epoch >= self.opt.dense_sample_epoch else False)
			# (NxHxW)xDx3, (NxHxW)xDx3, (NxHxW)xD, (NxHxW)x3
			mean, var, z_vals, ray_dir = mean.view([N, H, W, D, 3]), var.view([N, H, W, D, 3]), z_vals.view([N, H, W, D+1]), ray_dir.view([N, H, W, 3])
			H_idx = torch.randint(low=0, high=start_range, size=(1,), device=dev)
			W_idx = torch.randint(low=0, high=start_range, size=(1,), device=dev)
			z_vals_, ray_dir_ = z_vals[..., H_idx:H_idx + rs, W_idx:W_idx + rs, :], ray_dir[..., H_idx:H_idx + rs, W_idx:W_idx + rs, :]
			mean_, var_ = mean[:, H_idx:H_idx + rs, W_idx:W_idx + rs, ...], var[:, H_idx:H_idx + rs, W_idx:W_idx + rs, ...]
			mean, var, z_vals, ray_dir = mean_.flatten(0, 2), var_.flatten(0, 2), z_vals_.flatten(0, 2), ray_dir_.flatten(0, 2)
			x = self.x[:, :, H_idx:H_idx + rs, W_idx:W_idx + rs]
			if self.opt.depth_supervision and epoch >= self.opt.depth_in:
				disparity = self.disparity[:, :, H_idx:H_idx + rs, W_idx:W_idx + rs]
			self.z_vals, self.ray_dir = z_vals, ray_dir

		# local_locality_ratio = self.opt.obj_scale/self.opt.nss_scale if epoch >= self.opt.locality_in and epoch < self.opt.no_locality_epoch else None
		W, H, D = self.opt.supervision_size, self.opt.supervision_size, self.opt.n_samp if epoch < self.opt.dense_sample_epoch else self.opt.n_dense_samp
		fg_object_size = self.opt.fg_object_size / self.opt.nss_scale if epoch >= self.opt.dense_sample_epoch else None

		# scaled depth
		slot_depth = torch.ones_like(fg_slot_position[:, 0:1]).to(self.x.device) * self.opt.depth_scale
		fg_slot_nss_position = pixel2world(fg_slot_position, cam2world_viewer, intrinsics=self.intrinsics[0], 
												nss_scale=self.opt.nss_scale, depth=slot_depth) # Kx3

		raws, masked_raws, unmasked_raws, masks = self.netDecoder(mean, var, z_slots, nss2cam0, fg_slot_nss_position, 
							dens_noise=dens_noise, fg_object_size=fg_object_size, bg_rotate=self.opt.bg_rotate,
							)
		
		raws = raws.view([N, H, W, D, 4]).flatten(start_dim=0, end_dim=2)  # (NxHxW)xDx4
		masked_raws = masked_raws.view([K, N, H, W, D, 4])
		unmasked_raws = unmasked_raws.view([K, N, H, W, D, 4])
		masks = masks.view([K, N, H, W, D, 1])
		rgb_map, depth_map, _ = raw2outputs(raws, z_vals, ray_dir, mip=True)
		# (NxHxW)x3, (NxHxW)
		rendered = rgb_map.view(N, H, W, 3).permute([0, 3, 1, 2])  # Nx3xHxW
		x_recon = rendered * 2 - 1
		
		self.loss_recon = self.L2_loss(x_recon, x)
		x_norm, rendered_norm = self.vgg_norm((x + 1) / 2), self.vgg_norm(rendered)
		rendered_feat, x_feat = self.perceptual_net(rendered_norm), self.perceptual_net(x_norm)
		self.loss_perc = self.weight_percept * self.L2_loss(rendered_feat, x_feat)

		if self.collapse_prevent_iter > 0:
			collapse_prevent_weight = 2*self.collapse_prevent_iter
			if self.opt.bg_density_loss:
				bg_density = masks[0, ..., -1].flatten(start_dim=0, end_dim=2)  # (NxHxW)xD
				# option 1: penalize if the bg_density is too small
				self.loss_bg_density = collapse_prevent_weight * torch.clamp(5e-4 - torch.mean(bg_density), min=0)
				# option 2: penalize the near-camera region, first define a mask
				# n_penalize = int(D*(self.opt.bg_penalize_plane-self.opt.near_plane)/(self.opt.far_plane-self.opt.near_plane))
				# mask = torch.zeros_like(bg_density)
				# mask[:, :n_penalize] = 1
				# self.loss_bg_density = self.opt.weight_bg_density * torch.sum(bg_density * mask) / (N*n_penalize*H*W)
			if self.opt.fg_density_loss:
				fg_density = torch.sum(masks[1:, ..., -1], dim=0).flatten(start_dim=0, end_dim=2)  # (NxHxW)xD
				# penalize if fg_density if too small
				self.loss_fg_density = collapse_prevent_weight * torch.clamp(5e-4 - torch.mean(fg_density), min=0)
			self.collapse_prevent_iter -= 1

		if self.opt.depth_supervision and epoch >= self.opt.depth_in:
			depth_rendered = depth_map.view(N, H, W).clamp(min=1e-6).unsqueeze(1) # Nx1xHxW
			disparity_rendered = 1 / depth_rendered # Nx1xHxW
			''' add ranking loss, randomly pick a patch of length L, 
			and a nearby patch of length L (no more than 32 pixels away), forming L**2 pairs. For each pair of pixels,
			if the first pixel has smaller disparity than the second one on the ground truth, (i.e., disparity_1_gt < disparity_2_gt)
			then loss = max(0, depth_2_rendered - depth_1_rendered + margin)
			else loss = max(0, depth_1_rendered - depth_2_rendered + margin) '''
			L, diff_max = 16, 5
			margin = 1e-4

			patch1_start_h, patch1_start_w = torch.randint(low=diff_max, high=H-L-diff_max, size=(1,), device=dev), \
											torch.randint(low=diff_max, high=W-L-diff_max, size=(1,), device=dev)
			patch2_start_h, patch2_start_w = torch.randint(low=-diff_max, high=diff_max, size=(1,), device=dev) + patch1_start_h, \
											torch.randint(low=-diff_max, high=diff_max, size=(1,), device=dev) + patch1_start_w

			disparity_gt_1 = disparity[:, 0, patch1_start_h:patch1_start_h+L, patch1_start_w:patch1_start_w+L].flatten() # N*L**2
			disparity_gt_2 = disparity[:, 0, patch2_start_h:patch2_start_h+L, patch2_start_w:patch2_start_w+L].flatten() # N*L**2
			mask_gt = (disparity_gt_1 < disparity_gt_2).float()

			depth_rendered_1 = depth_rendered[:, 0, patch1_start_h:patch1_start_h+L, patch1_start_w:patch1_start_w+L].flatten() # N*L**2
			depth_rendered_2 = depth_rendered[:, 0, patch2_start_h:patch2_start_h+L, patch2_start_w:patch2_start_w+L].flatten() # N*L**2

			depth_diff_rendered = (depth_rendered_1 - depth_rendered_2)
			self.loss_depth_ranking += (torch.mean(torch.clamp(depth_diff_rendered + margin, min=0) * (1 - mask_gt)) + \
					  			torch.mean(torch.clamp(-depth_diff_rendered + margin, min=0) * mask_gt)) * self.opt.weight_depth_ranking		

		if self.opt.sfs_loss:
			# shape representations of foreground slots: z_slots[1:, :self.opt.shape_dim] # K*C
			# representations of spatial features: feat_shape.flatten(1, 2).squeeze(0) # N*C
			self.loss_sfs = self.opt.weight_sfs * self.sfs_loss(z_slots[1:, :self.opt.shape_dim], feat_shape.flatten(1, 2).squeeze(0))

		if self.opt.position_loss and epoch >= self.opt.position_in:
			# infer the position from the second view
			feat_shape_, feat_color_ = self.encode(1)
			_, _, fg_slot_position_ = self.netSlotAttention(feat=feat_shape_, feat_color=None,
							dropout_shape_rate=None, dropout_all_rate=None)  # 1xKx2

			fg_slot_position_ = fg_slot_position_.squeeze(0)  # Kx2
			if not (self.opt.scaled_depth or self.opt.scaled_depth_map):
				fg_slot_nss_position_ = pixel2world(fg_slot_position_, cam2world[1], intrinsics=self.intrinsics, nss_scale=self.opt.nss_scale)
			else:
				if self.opt.depth_scale_map:
					# retrerive the depth value for each slot position from the depth map self.disparity[1].squeeze() (H*W), use bilinear interpolation
					depth_map = self.disparity[1:2]
					slot_depth = F.grid_sample(depth_map, fg_slot_position_.unsqueeze(0).unsqueeze(2), mode='bilinear', align_corners=False) # 1*1*K*1
					slot_depth = slot_depth.squeeze(0).squeeze(0) * self.opt.depth_scale # K*1
				else:
					slot_depth = torch.ones_like(fg_slot_position_[:, 0:1]).to(self.x.device) * self.opt.depth_scale
				fg_slot_nss_position_ = pixel2world(fg_slot_position_, cam2world[1], intrinsics=self.intrinsics,
															nss_scale=self.opt.nss_scale, depth=slot_depth)
			# fg_slot_nss_position_ = pixel2world(fg_slot_position_, cam2world[1], intrinsics=self.intrinsics, nss_scale=self.opt.nss_scale,
			# 						depth=self.disparity[1].flatten()[fg_slot_position_[:, 0]*H + fg_slot_position_[:, 1]].unsqueeze(1) * scale) # K * 1	
			# calculate the position loss (L2 loss between the two inferred positions)
			self.loss_pos = self.opt.weight_position * self.pos_loss(fg_slot_nss_position, fg_slot_nss_position_)
			# print('position loss: {}'.format(self.loss_position.item()))

		with torch.no_grad():
			attn = attn.detach().cpu()  # KxN
			H_, W_ = feat_shape.shape[1:3]
			attn = attn.view(self.opt.num_slots, 1, H_, W_)
			# if H_ != H:
			# 	attn = F.interpolate(attn, size=[H, W], mode='bilinear')
			setattr(self, 'attn', attn)
			for i in range(self.opt.n_img_each_scene):
				setattr(self, 'x_rec{}'.format(i), x_recon[i])
				setattr(self, 'x{}'.format(i), x[i])
				if self.opt.depth_supervision and epoch >= self.opt.depth_in:
					# normalize to 0-1
					setattr(self, 'disparity_{}'.format(i), (disparity[i] - disparity[i].min()) / (disparity[i].max() - disparity[i].min()))
					setattr(self, 'disparity_rec{}'.format(i), (disparity_rendered[i] - disparity_rendered[i].min()) / (disparity_rendered[i].max() - disparity_rendered[i].min()))
					
			setattr(self, 'masked_raws', masked_raws.detach())
			setattr(self, 'unmasked_raws', unmasked_raws.detach())
			setattr(self, 'fg_slot_image_position', fg_slot_position.detach())
			setattr(self, 'fg_slot_nss_position', fg_slot_nss_position.detach())
			
	def compute_visuals(self):
		with torch.no_grad():
			_, N, H, W, D, _ = self.masked_raws.shape
			masked_raws = self.masked_raws  # KxNxHxWxDx4
			unmasked_raws = self.unmasked_raws  # KxNxHxWxDx4
			for k in range(self.num_slots):
				raws = masked_raws[k]  # NxHxWxDx4
				z_vals, ray_dir = self.z_vals, self.ray_dir
				raws = raws.flatten(start_dim=0, end_dim=2)  # (NxHxW)xDx4
				rgb_map, depth_map, _ = raw2outputs(raws, z_vals, ray_dir, mip=True)
				rendered = rgb_map.view(N, H, W, 3).permute([0, 3, 1, 2])  # Nx3xHxW
				x_recon = rendered * 2 - 1
				for i in range(self.opt.n_img_each_scene):
					setattr(self, 'slot{}_view{}'.format(k, i), x_recon[i])
				raws = unmasked_raws[k]  # NxHxWxDx4
				raws = raws.flatten(start_dim=0, end_dim=2)  # (NxHxW)xDx4
				rgb_map, depth_map, _ = raw2outputs(raws, z_vals, ray_dir, mip=True)
				rendered = rgb_map.view(N, H, W, 3).permute([0, 3, 1, 2])  # Nx3xHxW
				x_recon = rendered * 2 - 1
				for i in range(self.opt.n_img_each_scene):
					setattr(self, 'unmasked_slot{}_view{}'.format(k, i), x_recon[i])
				setattr(self, 'slot{}_attn'.format(k), self.attn[k] * 2 - 1)

			for k in range(self.num_slots, self.opt.num_slots):
				# add dummy images
				for i in range(self.opt.n_img_each_scene):
					setattr(self, 'slot{}_view{}'.format(k, i), torch.zeros_like(x_recon[i]))
					setattr(self, 'unmasked_slot{}_view{}'.format(k, i), torch.zeros_like(x_recon[i]))
				setattr(self, 'slot{}_attn'.format(k), self.attn[k] * 2 - 1)

	def backward(self):
		"""Calculate losses, gradients, and update network weights; called in every training iteration"""
		loss = self.loss_recon + self.loss_perc + self.loss_sfs + \
					self.loss_pos + self.loss_bg_density + self.loss_fg_density + self.loss_depth_ranking 
		loss.backward()
		# self.loss_perc = self.loss_perc / self.weight_percept if self.weight_percept > 0 else self.loss_perc

	def optimize_parameters(self, ret_grad=False, epoch=0):
		"""Update network weights; it will be called in every training iteration."""
		self.forward(epoch)
		for opm in self.optimizers:
			opm.zero_grad()
		self.backward()
		avg_grads = []
		layers = []
		if ret_grad:
			for n, p in chain(self.netEncoder.named_parameters(), self.netSlotAttention.named_parameters(), self.netDecoder.named_parameters()):
				if p.grad is not None and "bias" not in n:
					with torch.no_grad():
						layers.append(n)
						avg_grads.append(p.grad.abs().mean().cpu().item())
		for opm in self.optimizers:
			opm.step()
		return layers, avg_grads

	def save_networks(self, surfix):
		"""Save all the networks to the disk.
		Parameters:
			surfix (int or str) -- current epoch; used in the file name '%s_net_%s.pth' % (epoch, name)
		"""
		super().save_networks(surfix)
		for i, opm in enumerate(self.optimizers):
			save_filename = '{}_optimizer_{}.pth'.format(surfix, i)
			save_path = os.path.join(self.save_dir, save_filename)
			torch.save(opm.state_dict(), save_path)

		for i, sch in enumerate(self.schedulers):
			save_filename = '{}_lr_scheduler_{}.pth'.format(surfix, i)
			save_path = os.path.join(self.save_dir, save_filename)
			torch.save(sch.state_dict(), save_path)

	def load_networks(self, surfix):
		"""Load all the networks from the disk.
		Parameters:
			surfix (int or str) -- current epoch; used in he file name '%s_net_%s.pth' % (epoch, name)
		"""
		super().load_networks(surfix)

		if self.isTrain:
			for i, (opm, sch) in enumerate(zip(self.optimizers, self.schedulers)):
				load_opm_filename = '{}_optimizer_{}.pth'.format(surfix, i)
				load_sch_filename = '{}_lr_scheduler_{}.pth'.format(surfix, i)
				load_opm_path = os.path.join(self.save_dir, load_opm_filename)
				load_sch_path = os.path.join(self.save_dir, load_sch_filename)
				print('loading the optimizer from %s' % load_opm_path)
				print('loading the lr scheduler from %s' % load_sch_path)
				state_dict_opm = torch.load(load_opm_path, map_location=str(self.device))
				state_dict_sch = torch.load(load_sch_path, map_location=str(self.device))
				try:
					opm.load_state_dict(state_dict_opm)
					sch.load_state_dict(state_dict_sch)
				except:
					# pass
					n_steps = int(state_dict_sch['last_epoch'])
					for _ in range(n_steps):
						sch.step()

			# for i, opm in enumerate(self.optimizers):
			# 	load_filename = '{}_optimizer_{}.pth'.format(surfix, i)
			# 	load_path = os.path.join(self.save_dir, load_filename)
			# 	print('loading the optimizer from %s' % load_path)
			# 	state_dict = torch.load(load_path, map_location=str(self.device))
			# 	opm.load_state_dict(state_dict)

			# for i, sch in enumerate(self.schedulers):
			# 	load_filename = '{}_lr_scheduler_{}.pth'.format(surfix, i)
			# 	load_path = os.path.join(self.save_dir, load_filename)
			# 	print('loading the lr scheduler from %s' % load_path)
			# 	state_dict = torch.load(load_path, map_location=str(self.device))
			# 	sch.load_state_dict(state_dict)


if __name__ == '__main__':
	pass