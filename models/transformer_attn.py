import math
from os import X_OK

from .op import conv2d_gradfix
import torch
from torch import nn
import torch.nn.functional as F
from torch.nn import init
from torchvision.models import vgg16
from torch import autograd
from models.model_general import InputPosEmbedding
from .utils import PositionalEncoding, build_grid

class EncoderPosEmbedding(nn.Module):
	def __init__(self, dim, slot_dim, hidden_dim=128):
		super().__init__()
		self.grid_embed = nn.Linear(4, dim, bias=True)
		self.input_to_k_fg = nn.Linear(dim, dim, bias=False)
		self.input_to_v_fg = nn.Linear(dim, dim, bias=False)

		self.input_to_k_bg = nn.Linear(dim, dim, bias=False)
		self.input_to_v_bg = nn.Linear(dim, dim, bias=False)

		self.MLP_fg = nn.Linear(dim, slot_dim, bias=False)
		self.MLP_bg = nn.Linear(dim, slot_dim, bias=False)
		
	def apply_rel_position_scale(self, grid, position):
		"""
		grid: (1, h, w, 2)
		position (batch, number_slots, 2)
		"""
		b, n, _ = position.shape
		h, w = grid.shape[1:3]
		grid = grid.view(1, 1, h, w, 2)
		grid = grid.repeat(b, n, 1, 1, 1)
		position = position.view(b, n, 1, 1, 2)
		
		return grid - position # (b, n, h, w, 2)

	def forward(self, x, h, w, position_latent=None):

		grid = build_grid(h, w, x.device) # (1, h, w, 2)
		if position_latent is not None:
			rel_grid = self.apply_rel_position_scale(grid, position_latent)
		else:
			rel_grid = grid.unsqueeze(0).repeat(x.shape[0], 1, 1, 1, 1) # (b, 1, h, w, 2)

		# rel_grid = rel_grid.flatten(-3, -2) # (b, 1, h*w, 2)
		rel_grid = torch.cat([rel_grid, -rel_grid], dim=-1).flatten(-3, -2) # (b, n_slot-1, h*w, 4)
		grid_embed = self.grid_embed(rel_grid) # (b, n_slot-1, h*w, d)

		k, v = self.input_to_k_fg(x).unsqueeze(1), self.input_to_v_fg(x).unsqueeze(1)
		k, v = k + grid_embed, v + grid_embed
		k, v = self.MLP_fg(k), self.MLP_fg(v)

		return k, v # (b, n, h*w, d)

	def forward_bg(self, x, h, w):
		grid = build_grid(h, w, x.device) # (1, h, w, 2)
		rel_grid = grid.unsqueeze(0).repeat(x.shape[0], 1, 1, 1, 1) # (b, 1, h, w, 2)
		# rel_grid = rel_grid.flatten(-3, -2) # (b, 1, h*w, 2)
		rel_grid = torch.cat([rel_grid, -rel_grid], dim=-1).flatten(-3, -2) # (b, 1, h*w, 4)
		grid_embed = self.grid_embed(rel_grid) # (b, 1, h*w, d)
		
		k_bg, v_bg = self.input_to_k_bg(x).unsqueeze(1), self.input_to_v_bg(x).unsqueeze(1) # (b, 1, h*w, d)
		k_bg, v_bg = self.MLP_bg(k_bg + grid_embed), self.MLP_bg(v_bg + grid_embed)

		return k_bg, v_bg # (b, 1, h*w, d)

class SlotAttention(nn.Module):
	def __init__(self, num_slots, in_dim=64, slot_dim=64, color_dim=8, iters=4, eps=1e-8, hidden_dim=128,
		  learnable_pos=True, n_feats=64*64, global_feat=False, pos_emb=False, feat_dropout_dim=None,
		  random_init_pos=False, pos_no_grad=False, learnable_init=False, dropout=0.,):
		super().__init__()
		self.num_slots = num_slots
		self.iters = iters
		self.eps = eps
		self.scale = slot_dim ** -0.5

		self.learnable_init = learnable_init

		if not self.learnable_init:
			self.slots_mu = nn.Parameter(torch.randn(1, 1, slot_dim))
			self.slots_logsigma = nn.Parameter(torch.zeros(1, 1, slot_dim))
			init.xavier_uniform_(self.slots_logsigma)
			self.slots_mu_bg = nn.Parameter(torch.randn(1, 1, slot_dim))
			self.slots_logsigma_bg = nn.Parameter(torch.zeros(1, 1, slot_dim))
			init.xavier_uniform_(self.slots_logsigma_bg)
		else:
			self.slots_init_fg = nn.Parameter((torch.randn(1, num_slots-1, slot_dim)))
			self.slots_init_bg = nn.Parameter((torch.randn(1, 1, slot_dim)))
			init.xavier_uniform_(self.slots_init_fg)
			init.xavier_uniform_(self.slots_init_bg)

		self.learnable_pos = learnable_pos
		self.fg_position = nn.Parameter(torch.rand(1, num_slots-1, 2) * 1.5 - 0.75)

		if self.learnable_pos:
			self.attn_to_pos_bias = nn.Sequential(nn.Linear(n_feats, 2), nn.Tanh()) # range (-1, 1)
			self.attn_to_pos_bias[0].weight.data.zero_()
			self.attn_to_pos_bias[0].bias.data.zero_()

		self.to_kv = EncoderPosEmbedding(in_dim, slot_dim)
		self.to_q = nn.Sequential(nn.LayerNorm(slot_dim), nn.Linear(slot_dim, slot_dim, bias=False))
		self.to_q_bg = nn.Sequential(nn.LayerNorm(slot_dim), nn.Linear(slot_dim, slot_dim, bias=False))

		self.norm_feat = nn.LayerNorm(in_dim)
		if color_dim != 0:
			self.norm_feat_color = nn.LayerNorm(color_dim)
		self.slot_dim = slot_dim

		self.global_feat = global_feat
		self.random_init_pos = random_init_pos
		self.pos_no_grad = pos_no_grad
		self.pos_emb = pos_emb
		if self.pos_emb:
			self.input_pos_emb = InputPosEmbedding(in_dim)
		self.dropout_shape_dim = feat_dropout_dim

		self.dropout = nn.Dropout(dropout)

	def forward(self, feat, feat_color=None, num_slots=None, dropout_shape_rate=None, 
			dropout_all_rate=None, init_mask=None):
		"""
		input:
			feat: visual feature with position information, BxHxWxC
			feat_color: texture feature with position information, BxHxWxC'
			output: slots: BxKxC, attn: BxKxN
		"""
		B, H, W, _ = feat.shape
		N = H * W
		if self.pos_emb:
			feat = self.input_pos_emb(feat)
		feat = feat.flatten(1, 2) # (B, N, C)

		if init_mask is not None:
			init_mask = F.interpolate(init_mask, size=(H, W), mode='bilinear', align_corners=False) # (K-1, 1, H, W)
			if init_mask.shape[0] != self.num_slots - 1:
				init_mask = torch.cat([init_mask, torch.ones(self.num_slots - 1 - init_mask.shape[0], 1, H, W, device=init_mask.device)], dim=0)
			init_mask = init_mask.flatten(1,3).unsqueeze(0).expand(B, -1, -1) # (B, K-1, N)

		K = num_slots if num_slots is not None else self.num_slots

		if not self.learnable_init:
			mu = self.slots_mu.expand(B, K-1, -1)
			sigma = self.slots_logsigma.exp().expand(B, K-1, -1)
			slot_fg = mu + sigma * torch.randn_like(mu)

			mu_bg = self.slots_mu_bg.expand(B, 1, -1)
			sigma_bg = self.slots_logsigma_bg.exp().expand(B, 1, -1)
			slot_bg = mu_bg + sigma_bg * torch.randn_like(mu_bg)
		else:
			slot_fg = self.slots_init_fg.expand(B, K-1, -1)
			slot_bg = self.slots_init_bg.expand(B, 1, -1)
		
		feat = self.norm_feat(feat)
		if feat_color is not None:
			feat_color = self.norm_feat_color(feat_color)

		k_bg, v_bg = self.to_kv.forward_bg(feat, H, W) # (B,1,N,C)

		grid = build_grid(H, W, device=feat.device).flatten(1, 2) # (1,N,2)
		
		fg_position = self.fg_position if (self.fg_position is not None and not self.random_init_pos) else torch.zeros(1, K-1, 2).to(feat.device)
		fg_position = fg_position.expand(B, -1, -1)[:, :K-1, :].to(feat.device) # Bx(K-1)x2

		# attn = None
		for it in range(self.iters):
			slot_prev_bg = slot_bg
			slot_prev_fg = slot_fg
			q_fg = self.to_q(slot_fg)
			q_bg = self.to_q_bg(slot_bg) # (B,1,C)
			
			attn = torch.empty(B, K, N, device=feat.device)
			
			k, v = self.to_kv(feat, H, W, fg_position, 
			 			dropout_shape_dim=self.dropout_shape_dim, 
			 			dropout_shape_rate=dropout_shape_rate,
						dropout_all_rate=dropout_all_rate) # (B,K-1,N,C), (B,K-1,N,C)
			
			for i in range(K):
				if i != 0:
					k_i = k[:, i-1] # (B,N,C)
					slot_qi = q_fg[:, i-1] # (B,C)
					attn[:, i] = torch.einsum('bd,bnd->bn', slot_qi, k_i) * self.scale
				else:
					attn[:, i] = torch.einsum('bd,bnd->bn', q_bg.squeeze(1), k_bg.squeeze(1)) * self.scale
			
			attn = attn.softmax(dim=1) + self.eps  # BxKxN
			attn_fg, attn_bg = attn[:, 1:, :], attn[:, 0:1, :]  # Bx(K-1)xN, Bx1xN
			attn_weights_fg = attn_fg / attn_fg.sum(dim=-1, keepdim=True)  # Bx(K-1)xN
			attn_weights_bg = attn_bg / attn_bg.sum(dim=-1, keepdim=True)  # Bx1xN
			
			# update slot position
			# print(attn_weights_fg.shape, grid.shape, fg_position.shape)
			if init_mask is not None: # (K, 1, H, W)
				fg_position = torch.einsum('bkn,bnd->bkd', init_mask, grid) # (B,K-1,N) * (B,N,2) -> (B,K-1,2)
			else:
				if self.pos_no_grad:
					with torch.no_grad():
						fg_position = torch.einsum('bkn,bnd->bkd', attn_weights_fg, grid) # (B,K-1,N) * (B,N,2) -> (B,K-1,2)
				else:
					fg_position = torch.einsum('bkn,bnd->bkd', attn_weights_fg, grid) # (B,K-1,N) * (B,N,2) -> (B,K-1,2)

			if self.learnable_pos: # add a bias term
				fg_position = fg_position * 0.8 + self.attn_to_pos_bias(attn_weights_fg) * 0.2 # (B,K-1,2)

			if it != self.iters - 1:
				updates_fg = torch.empty(B, K-1, self.slot_dim, device=k.device) # (B,K-1,C)
				for i in range(K-1):
					v_i = v[:, i] # (B,N,C)
					attn_i = attn_weights_fg[:, i] # (B,N)
					updates_fg[:, i] = torch.einsum('bn,bnd->bd', attn_i, v_i)

				updates_bg = torch.einsum('bn,bnd->bd',attn_weights_bg.squeeze(1), v_bg.squeeze(1)) # (B,N,C) * (B,N) -> (B,C)
				updates_bg = updates_bg.unsqueeze(1) # (B,1,C)

				slot_bg = slot_prev_bg + self.dropout(updates_bg)
				slot_fg = slot_prev_fg + self.dropout(updates_fg)

			else:
				if feat_color is not None:
					# calculate slot color feature
					feat_color = feat_color.flatten(1, 2) # (B,N,C')
					if not self.global_feat:
						slot_fg_color = torch.einsum('bkn,bnd->bkd', attn_weights_fg, feat_color) # (B,K-1,N) * (B,N,C') -> (B,K-1,C')
						slot_bg_color = torch.einsum('bn,bnd->bd', attn_weights_bg.squeeze(1), feat_color).unsqueeze(1) # (B,N) * (B,N,C') -> (B,C'), (B,1,C')
					else:
						slot_fg_color = feat_color.repeat(1, K-1, 1) # (B,K-1,C')
						slot_bg_color = feat_color

		if feat_color is not None:
			slot_fg = torch.cat([slot_fg, slot_fg_color], dim=-1) # (B,K-1,C+C')
			slot_bg = torch.cat([slot_bg, slot_bg_color], dim=-1) # (B,1,C+C')
			
		slots = torch.cat([slot_bg, slot_fg], dim=1) # (B,K,C+C')
				
		return slots, attn, fg_position
	
class SlotAttentionTF(nn.Module):
	def __init__(self, num_slots, in_dim=64, slot_dim=64, color_dim=8, iters=4, eps=1e-8, hidden_dim=128,
		  learnable_pos=True, n_feats=64*64, global_feat=False, feat_dropout_dim=None,
		  dropout=0., momentum=0.5, pos_init='learnable'):
		super().__init__()
		self.num_slots = num_slots
		self.iters = iters
		self.eps = eps
		self.scale = slot_dim ** -0.5
		self.pos_momentum = momentum
		self.pos_init = pos_init

		if self.pos_init == 'learnable':
			self.fg_position = nn.Parameter(torch.rand(1, num_slots-1, 2) * 1.5 - 0.75)
		
		self.slots_init_fg = nn.Parameter((torch.randn(1, num_slots-1, slot_dim)))
		self.slots_init_bg = nn.Parameter((torch.randn(1, 1, slot_dim)))

		self.learnable_pos = learnable_pos

		if self.learnable_pos:
			self.attn_to_pos_bias = nn.Sequential(nn.Linear(n_feats, 2), nn.Tanh()) # range (-1, 1)
			self.attn_to_pos_bias[0].weight.data.zero_()
			self.attn_to_pos_bias[0].bias.data.zero_()

		self.to_kv = EncoderPosEmbedding(in_dim, slot_dim)
		self.to_q = nn.Sequential(nn.LayerNorm(slot_dim), nn.Linear(slot_dim, slot_dim, bias=False))
		self.to_q_bg = nn.Sequential(nn.LayerNorm(slot_dim), nn.Linear(slot_dim, slot_dim, bias=False))

		self.norm_feat = nn.LayerNorm(in_dim)
		if color_dim != 0:
			self.norm_feat_color = nn.LayerNorm(color_dim)
		self.slot_dim = slot_dim

		self.global_feat = global_feat

		self.dropout_shape_dim = feat_dropout_dim

		self.dropout = nn.Dropout(dropout)

	def forward(self, feat, feat_color=None, num_slots=None, dropout_shape_rate=None, 
			dropout_all_rate=None):
		"""
		input:
			feat: visual feature with position information, BxHxWxC
			feat_color: texture feature with position information, BxHxWxC'
			output: slots: BxKxC, attn: BxKxN
		"""
		B, H, W, _ = feat.shape
		N = H * W
		feat = feat.flatten(1, 2) # (B, N, C)

		K = num_slots if num_slots is not None else self.num_slots
		
		if self.pos_init == 'learnable':
			fg_position = self.fg_position.expand(B, -1, -1).to(feat.device)
		elif self.pos_init == 'random':
			fg_position = torch.rand(B, K-1, 2, device=feat.device) * 1.8 - 0.9 # (B, K-1, 2)
		else: # zero init
			fg_position = torch.zeros(B, K-1, 2, device=feat.device)

		slot_fg = self.slots_init_fg.expand(B, -1, -1) # (B, K-1, C)
		slot_bg = self.slots_init_bg.expand(B, 1, -1) # (B, 1, C)
		
		feat = self.norm_feat(feat)
		if feat_color is not None:
			feat_color = self.norm_feat_color(feat_color)

		k_bg, v_bg = self.to_kv.forward_bg(feat, H, W) # (B,1,N,C)

		grid = build_grid(H, W, device=feat.device).flatten(1, 2) # (1,N,2)

		# attn = None
		for it in range(self.iters):
			fg_position_prev = fg_position
			slot_prev_bg = slot_bg
			slot_prev_fg = slot_fg
			q_fg = self.to_q(slot_fg)
			q_bg = self.to_q_bg(slot_bg) # (B,1,C)
			
			attn = torch.empty(B, K, N, device=feat.device)
			
			k, v = self.to_kv(feat, H, W, fg_position, 
			 			dropout_shape_dim=self.dropout_shape_dim, 
			 			dropout_shape_rate=dropout_shape_rate,
						dropout_all_rate=dropout_all_rate) # (B,K-1,N,C), (B,K-1,N,C)
			
			for i in range(K):
				if i != 0:
					k_i = k[:, i-1] # (B,N,C)
					slot_qi = q_fg[:, i-1] # (B,C)
					attn[:, i] = torch.einsum('bd,bnd->bn', slot_qi, k_i) * self.scale
				else:
					attn[:, i] = torch.einsum('bd,bnd->bn', q_bg.squeeze(1), k_bg.squeeze(1)) * self.scale
			
			attn = attn.softmax(dim=1) + self.eps  # BxKxN
			attn_fg, attn_bg = attn[:, 1:, :], attn[:, 0:1, :]  # Bx(K-1)xN, Bx1xN
			attn_weights_fg = attn_fg / attn_fg.sum(dim=-1, keepdim=True)  # Bx(K-1)xN
			attn_weights_bg = attn_bg / attn_bg.sum(dim=-1, keepdim=True)  # Bx1xN
			
			# momentum update slot position
			fg_position = torch.einsum('bkn,bnd->bkd', attn_weights_fg, grid) # (B,K-1,N) * (B,N,2) -> (B,K-1,2)
			fg_position = fg_position * (1 - self.pos_momentum) + fg_position_prev * self.pos_momentum

			if it != self.iters - 1:
				updates_fg = torch.empty(B, K-1, self.slot_dim, device=k.device) # (B,K-1,C)
				for i in range(K-1):
					v_i = v[:, i] # (B,N,C)
					attn_i = attn_weights_fg[:, i] # (B,N)
					updates_fg[:, i] = torch.einsum('bn,bnd->bd', attn_i, v_i)

				updates_bg = torch.einsum('bn,bnd->bd',attn_weights_bg.squeeze(1), v_bg.squeeze(1)) # (B,N,C) * (B,N) -> (B,C)
				updates_bg = updates_bg.unsqueeze(1) # (B,1,C)

				slot_bg = slot_prev_bg + self.dropout(updates_bg)
				slot_fg = slot_prev_fg + self.dropout(updates_fg)

			else:
				if self.learnable_pos: # add a bias term
					fg_position = fg_position + self.attn_to_pos_bias(attn_weights_fg) * 0.1 # (B,K-1,2)
					# fg_position = fg_position.clamp(-1, 1) # (B,K-1,2)
					
				if feat_color is not None:
					# calculate slot color feature
					feat_color = feat_color.flatten(1, 2) # (B,N,C')
					if not self.global_feat:
						slot_fg_color = torch.einsum('bkn,bnd->bkd', attn_weights_fg, feat_color) # (B,K-1,N) * (B,N,C') -> (B,K-1,C')
						slot_bg_color = torch.einsum('bn,bnd->bd', attn_weights_bg.squeeze(1), feat_color).unsqueeze(1) # (B,N) * (B,N,C') -> (B,C'), (B,1,C')
					else:
						slot_fg_color = feat_color.repeat(1, K-1, 1) # (B,K-1,C')
						slot_bg_color = feat_color

		if feat_color is not None:
			slot_fg = torch.cat([slot_fg, slot_fg_color], dim=-1) # (B,K-1,C+C')
			slot_bg = torch.cat([slot_bg, slot_bg_color], dim=-1) # (B,1,C+C')
			
		slots = torch.cat([slot_bg, slot_fg], dim=1) # (B,K,C+C')
				
		return slots, attn, fg_position

class AdaLN(nn.Module):
	def __init__(self, cond_dim, input_dim, condition=False):
		super().__init__()
		self.norm = nn.LayerNorm(input_dim)

		if condition:
			self.cond_fc = nn.Sequential(nn.Linear(cond_dim, input_dim*2, bias=True), nn.Tanh())
			self.cond_fc[0].weight.data.zero_()
			self.cond_fc[0].bias.data.zero_()
		else:
			self.cond_fc = None

	def forward(self, x, cond=None):
		"""
		x: (B, input_dim)
		cond: (B, cond_dim)
		return: (B, input_dim), input after AdaLN
		"""
		x = self.norm(x)

		if self.cond_fc is None or cond is None:
			return x
		else:
			cond_gamma, cond_beta = self.cond_fc(cond).chunk(2, dim=-1)
			return x * (1 + cond_gamma) + cond_beta

class SlotAttentionTransformer(nn.Module):
	def __init__(self, num_slots, in_dim=64, slot_dim=64, color_dim=8, iters=4, eps=1e-8,
		  learnable_pos=True, n_feats=64*64, global_feat=False, 
		  momentum=0.5, pos_init='learnable', camera_dim=5, camera_modulation=False):
		super().__init__()
		self.num_slots = num_slots
		self.iters = iters
		self.eps = eps
		self.scale = slot_dim ** -0.5
		self.pos_momentum = momentum
		self.pos_init = pos_init

		if self.pos_init == 'learnable':
			self.fg_position = nn.Parameter(torch.rand(1, num_slots-1, 2) * 1.5 - 0.75)
		
		self.slots_init_fg = nn.Parameter((torch.randn(1, num_slots-1, slot_dim)))
		self.slots_init_bg = nn.Parameter((torch.randn(1, 1, slot_dim)))

		self.learnable_pos = learnable_pos

		if self.learnable_pos:
			self.attn_to_pos_bias = nn.Sequential(nn.Linear(n_feats, 2), nn.Tanh()) # range (-1, 1)
			self.attn_to_pos_bias[0].weight.data.zero_()
			self.attn_to_pos_bias[0].bias.data.zero_()

		self.to_kv = EncoderPosEmbedding(in_dim, slot_dim)

		self.to_q_fg_AdaLN = AdaLN(camera_dim, slot_dim, condition=camera_modulation)
		self.to_q_fg =  nn.Linear(slot_dim, slot_dim, bias=False)
		self.to_q_bg_AdaLN = AdaLN(camera_dim, slot_dim, condition=camera_modulation)
		self.to_q_bg =  nn.Linear(slot_dim, slot_dim, bias=False)

		self.norm_feat = nn.LayerNorm(in_dim)
		if color_dim != 0:
			self.norm_feat_color = nn.LayerNorm(color_dim)
		self.slot_dim = slot_dim
		self.global_feat = global_feat

		self.mlp_fg_AdaLN = AdaLN(camera_dim, slot_dim, condition=camera_modulation)
		self.mlp_fg = nn.Sequential(nn.Linear(slot_dim, slot_dim), 
							  nn.GELU(), nn.Linear(slot_dim, slot_dim))
		self.mlp_bg_AdaLN = AdaLN(camera_dim, slot_dim, condition=camera_modulation)
		self.mlp_bg = nn.Sequential(nn.Linear(slot_dim, slot_dim),
							  nn.GELU(), nn.Linear(slot_dim, slot_dim))


	def forward(self, feat, camera_modulation, feat_color=None, num_slots=None):
		"""
		input:
			feat: visual feature with position information, BxHxWxC
			feat_color: texture feature with position information, BxHxWxC'
			output: slots: BxKxC, attn: BxKxN
		"""
		B, H, W, _ = feat.shape
		N = H * W
		feat = feat.flatten(1, 2) # (B, N, C)

		K = num_slots if num_slots is not None else self.num_slots
		
		if self.pos_init == 'learnable':
			fg_position = self.fg_position.expand(B, -1, -1).to(feat.device)
		elif self.pos_init == 'random':
			fg_position = torch.rand(B, K-1, 2, device=feat.device) * 1.8 - 0.9 # (B, K-1, 2)
		else: # zero init
			fg_position = torch.zeros(B, K-1, 2, device=feat.device)

		slot_fg = self.slots_init_fg.expand(B, -1, -1) # (B, K-1, C)
		slot_bg = self.slots_init_bg.expand(B, 1, -1) # (B, 1, C)
		
		feat = self.norm_feat(feat)

		k_bg, v_bg = self.to_kv.forward_bg(feat, H, W) # (B,1,N,C)

		grid = build_grid(H, W, device=feat.device).flatten(1, 2) # (1,N,2)

		# attn = None
		for it in range(self.iters):
			fg_position_prev = fg_position
			slot_prev_bg = slot_bg
			slot_prev_fg = slot_fg
			q_fg = self.to_q_fg(self.to_q_fg_AdaLN(slot_fg, camera_modulation)) # (B,K-1,C)
			q_bg = self.to_q_bg(self.to_q_bg_AdaLN(slot_bg, camera_modulation)) # (B,1,C)
			
			attn = torch.empty(B, K, N, device=feat.device)
			
			k, v = self.to_kv(feat, H, W, fg_position) # (B,K-1,N,C), (B,K-1,N,C)
			
			for i in range(K):
				if i != 0:
					k_i = k[:, i-1] # (B,N,C)
					slot_qi = q_fg[:, i-1] # (B,C)
					attn[:, i] = torch.einsum('bd,bnd->bn', slot_qi, k_i) * self.scale
				else:
					attn[:, i] = torch.einsum('bd,bnd->bn', q_bg.squeeze(1), k_bg.squeeze(1)) * self.scale
			
			attn = attn.softmax(dim=1) + self.eps  # BxKxN
			attn_fg, attn_bg = attn[:, 1:, :], attn[:, 0:1, :]  # Bx(K-1)xN, Bx1xN
			attn_weights_fg = attn_fg / attn_fg.sum(dim=-1, keepdim=True)  # Bx(K-1)xN
			attn_weights_bg = attn_bg / attn_bg.sum(dim=-1, keepdim=True)  # Bx1xN
			
			# momentum update slot position
			fg_position = torch.einsum('bkn,bnd->bkd', attn_weights_fg, grid) # (B,K-1,N) * (B,N,2) -> (B,K-1,2)
			fg_position = fg_position * (1 - self.pos_momentum) + fg_position_prev * self.pos_momentum

			if it != self.iters - 1:
				updates_fg = torch.empty(B, K-1, self.slot_dim, device=k.device) # (B,K-1,C)
				for i in range(K-1):
					v_i = v[:, i] # (B,N,C)
					attn_i = attn_weights_fg[:, i] # (B,N)
					updates_fg[:, i] = torch.einsum('bn,bnd->bd', attn_i, v_i)

				updates_bg = torch.einsum('bn,bnd->bd',attn_weights_bg.squeeze(1), v_bg.squeeze(1)) # (B,N,C) * (B,N) -> (B,C)
				updates_bg = updates_bg.unsqueeze(1) # (B,1,C)

				slot_bg = slot_prev_bg + updates_bg
				slot_fg = slot_prev_fg + updates_fg

				slot_bg = slot_bg + self.mlp_bg(self.mlp_bg_AdaLN(slot_bg, camera_modulation))
				slot_fg = slot_fg + self.mlp_fg(self.mlp_fg_AdaLN(slot_fg, camera_modulation))

			else:
				if self.learnable_pos: # add a bias term
					fg_position = fg_position + self.attn_to_pos_bias(attn_weights_fg) * 0.1 # (B,K-1,2)
					# fg_position = fg_position.clamp(-1, 1) # (B,K-1,2)
					
				if feat_color is not None:
					# calculate slot color feature
					feat_color = self.norm_feat_color(feat_color)
					feat_color = feat_color.flatten(1, 2) # (B,N,C')
					if not self.global_feat:
						slot_fg_color = torch.einsum('bkn,bnd->bkd', attn_weights_fg, feat_color) # (B,K-1,N) * (B,N,C') -> (B,K-1,C')
						slot_bg_color = torch.einsum('bn,bnd->bd', attn_weights_bg.squeeze(1), feat_color).unsqueeze(1) # (B,N) * (B,N,C') -> (B,C'), (B,1,C')
					else:
						slot_fg_color = feat_color.repeat(1, K-1, 1) # (B,K-1,C')
						slot_bg_color = feat_color

		if feat_color is not None:
			slot_fg = torch.cat([slot_fg, slot_fg_color], dim=-1) # (B,K-1,C+C')
			slot_bg = torch.cat([slot_bg, slot_bg_color], dim=-1) # (B,1,C+C')
			
		slots = torch.cat([slot_bg, slot_fg], dim=1) # (B,K,C+C')
				
		return slots, attn, fg_position

class DecoderIPE(nn.Module):
	def __init__(self, n_freq=5, input_dim=33+64, z_dim=64, n_layers=3, locality=True, 
		  			locality_ratio=4/7, fixed_locality=False, predict_depth_scale=False):
		"""
		freq: raised frequency
		input_dim: pos emb dim + slot dim
		z_dim: network latent dim
		n_layers: #layers before/after skip connection.
		locality: if True, for each obj slot, clamp sigma values to 0 outside obj_scale.
		locality_ratio: if locality, what value is the boundary to clamp?
		fixed_locality: if True, compute locality in world space instead of in transformed view space
		"""
		super().__init__()
		super().__init__()
		self.n_freq = n_freq
		self.locality = locality
		self.locality_ratio = locality_ratio
		self.fixed_locality = fixed_locality
		assert self.fixed_locality == True
		self.out_ch = 4
		self.z_dim = z_dim
		before_skip = [nn.Linear(input_dim, z_dim), nn.ReLU(True)]
		after_skip = [nn.Linear(z_dim+input_dim, z_dim), nn.ReLU(True)]
		for i in range(n_layers-1):
			before_skip.append(nn.Linear(z_dim, z_dim))
			before_skip.append(nn.ReLU(True))
			after_skip.append(nn.Linear(z_dim, z_dim))
			after_skip.append(nn.ReLU(True))
		self.f_before = nn.Sequential(*before_skip)
		self.f_after = nn.Sequential(*after_skip)
		self.f_after_latent = nn.Linear(z_dim, z_dim)
		self.f_after_shape = nn.Linear(z_dim, self.out_ch - 3)
		self.f_color = nn.Sequential(nn.Linear(z_dim, z_dim//4),
									 nn.ReLU(True),
									 nn.Linear(z_dim//4, 3))
		before_skip = [nn.Linear(input_dim, z_dim), nn.ReLU(True)]
		after_skip = [nn.Linear(z_dim + input_dim, z_dim), nn.ReLU(True)]
		for i in range(n_layers - 1):
			before_skip.append(nn.Linear(z_dim, z_dim))
			before_skip.append(nn.ReLU(True))
			after_skip.append(nn.Linear(z_dim, z_dim))
			after_skip.append(nn.ReLU(True))
		after_skip.append(nn.Linear(z_dim, self.out_ch))
		self.b_before = nn.Sequential(*before_skip)
		self.b_after = nn.Sequential(*after_skip)

		self.pos_enc = PositionalEncoding(max_deg=n_freq)

		if predict_depth_scale:
			self.scale = nn.Parameter(torch.tensor(1.0))

	def processQueries(self, mean, var, fg_transform, fg_slot_position, z_fg, z_bg, fg_object_size=None,
					rel_pos=True, bg_rotate=False):
		'''
		Process the query points and the slot features
		1. If self.fg_object_size is not None, do:
			Remove the query point that is too far away from the slot center, 
			the bouding box is defined as a cube with side length 2 * self.fg_object_size
			for the points outside the bounding box, keep only keep_ratio of them
			store the new sampling_coor_fg and the indices of the remaining points
		2. Do the pos emb by Fourier
		3. Concatenate the pos emb and the slot features
		4. If self.fg_object_size is not None, return the new sampling_coor_fg and their indices

		input: 	mean: PxDx3
				var: PxDx3
				fg_transform: 1x4x4
				fg_slot_position: (K-1)x3
				z_fg: (K-1)xC
				z_bg: 1xC
				ssize: supervision size (64)
				mask_ratio: frequency mask ratio to the pos emb
				rel_pos: use relative position to fg_slot_position or not
				bg_rotate: whether to rotate the background points to the camera coordinate
		return: input_fg: M * (60 + C) (M is the number of query points inside bbox), C is the slot feature dim, and 60 means increased-freq feat dim
				input_bg: Px(60+C)
				idx: M (indices of the query points inside bbox)
		'''
		P, D = mean.shape[0], mean.shape[1]
		K = z_fg.shape[0] + 1

		# only keep the points that inside the cube, ((K-1)*P*D)
		mask_locality = (torch.norm(mean.flatten(0,1), dim=-1) < self.locality_ratio).expand(K-1, -1).flatten(0, 1) if self.locality else torch.ones((K-1)*P*D, device=mean.device).bool()
		# mask_locality = torch.all(torch.abs(mean.flatten(0,1)) < self.locality_ratio, dim=-1).expand(K-1, -1).flatten(0, 1) if self.locality else torch.ones((K-1)*P*D, device=mean.device).bool()
		
		sampling_mean_fg = mean[None, ...].expand(K-1, -1, -1, -1).flatten(1, 2) # (K-1)*(P*D)*3

		if rel_pos:
			sampling_mean_fg = torch.cat([sampling_mean_fg, torch.ones_like(sampling_mean_fg[:, :, 0:1])], dim=-1)  # (K-1)*(P*D)*4
			sampling_mean_fg = torch.matmul(fg_transform[None, ...], sampling_mean_fg[..., None]).squeeze(-1)  # (K-1)*(P*D)*4
			sampling_mean_fg = sampling_mean_fg[:, :, :3]  # (K-1)*(P*D)*3
			
			fg_slot_position = torch.cat([fg_slot_position, torch.ones_like(fg_slot_position[:, 0:1])], dim=-1)  # (K-1)x4
			fg_slot_position = torch.matmul(fg_transform.squeeze(0), fg_slot_position.t()).t() # (K-1)x4
			fg_slot_position = fg_slot_position[:, :3]  # (K-1)x3

			sampling_mean_fg = sampling_mean_fg - fg_slot_position[:, None, :]  # (K-1)x(P*D)x3

		sampling_mean_fg = sampling_mean_fg.view([K-1, P, D, 3]).flatten(0, 1)  # ((K-1)xP)xDx3
		sampling_var_fg = var[None, ...].expand(K-1, -1, -1, -1).flatten(0, 1)  # ((K-1)xP)xDx3

		sampling_mean_bg, sampling_var_bg = mean, var

		if bg_rotate:
			sampling_mean_bg = torch.matmul(fg_transform[:, :3, :3], sampling_mean_bg[..., None]).squeeze(-1)  # PxDx3

		# 1. Remove the query points too far away from the slot center
		if fg_object_size is not None:
			sampling_mean_fg_ = sampling_mean_fg.flatten(start_dim=0, end_dim=1)  # ((K-1)xPxD)x3
			mask = torch.all(torch.abs(sampling_mean_fg_) < fg_object_size, dim=-1)  # ((K-1)xPxD) --> M
			mask = mask & mask_locality
			if mask.sum() <= 1:
				mask[:2] = True # M == 0 / 1, keep at least two points to avoid error
			idx = mask.nonzero().squeeze()  # Indices of valid points
		else:
			idx = mask_locality.nonzero().squeeze()
			# print('mask ratio: ', 1 - mask_locality.sum().item() / (K-1) / P / D)

		# 2. Compute Fourier position embeddings
		pos_emb_fg = self.pos_enc(sampling_mean_fg, sampling_var_fg)[0]  # ((K-1)xP)xDx(6*n_freq+3)
		pos_emb_bg = self.pos_enc(sampling_mean_bg, sampling_var_bg)[0]  # PxDx(6*n_freq+3)

		pos_emb_fg, pos_emb_bg = pos_emb_fg.flatten(0, 1)[idx], pos_emb_bg.flatten(0, 1)  # Mx(6*n_freq+3), (P*D)x(6*n_freq+3)

		# 3. Concatenate the embeddings with z_fg and z_bg features
		# Assuming z_fg and z_bg are repeated for each query point
		# Also assuming K is the first dimension of z_fg and we need to repeat it for each query point
		
		z_fg = z_fg[:, None, :].expand(-1, P*D, -1).flatten(start_dim=0, end_dim=1)  # ((K-1)xPxD)xC
		z_fg = z_fg[idx]  # MxC

		input_fg = torch.cat([pos_emb_fg, z_fg], dim=-1)
		input_bg = torch.cat([pos_emb_bg, z_bg.repeat(P*D, 1)], dim=-1) # (P*D)x(6*n_freq+3+C)

		# 4. Return required tensors
		return input_fg, input_bg, idx

	def forward(self, mean, var, z_slots, fg_transform, fg_slot_position, dens_noise=0., 
		 			fg_object_size=None, rel_pos=True, bg_rotate=False):
		"""
		1. pos emb by Fourier
		2. for each slot, decode all points from coord and slot feature
		input:
			mean: P*D*3, P = (N*H*W)
			var: P*D*3, P = (N*H*W)
			view_dirs: P*3, P = (N*H*W)
			z_slots: KxC, K: #slots, C: #feat_dim
			z_slots_texture: KxC', K: #slots, C: #texture_dim
			fg_transform: If self.fixed_locality, it is 1x4x4 matrix nss2cam0 in nss space,
							otherwise it is 1x3x3 azimuth rotation of nss2cam0 (not used)
			fg_slot_position: (K-1)x3 in nss space
			dens_noise: Noise added to density

			if fg_slot_cam_position is not None, we should first project it world coordinates
			depth: K*1, depth of the slots
		"""
		K, C = z_slots.shape
		P, D = mean.shape[0], mean.shape[1]

		# if self.locality:
		# 	outsider_idx = torch.any(mean.flatten(0,1).abs() > self.locality_ratio, dim=-1).unsqueeze(0).expand(K-1, -1) # (K-1)x(P*D)

		z_bg = z_slots[0:1, :]  # 1xC
		z_fg = z_slots[1:, :]  # (K-1)xC

		input_fg, input_bg, idx = self.processQueries(mean, var, fg_transform, fg_slot_position, z_fg, z_bg, 
						fg_object_size=fg_object_size, rel_pos=rel_pos, bg_rotate=bg_rotate)
		
		tmp = self.b_before(input_bg)
		bg_raws = self.b_after(torch.cat([input_bg, tmp], dim=1)).view([1, P*D, self.out_ch])  # (P*D)x4 -> 1x(P*D)x4

		tmp = self.f_before(input_fg)
		tmp = self.f_after(torch.cat([input_fg, tmp], dim=1))  # Mx64

		latent_fg = self.f_after_latent(tmp)  # Mx64
		fg_raw_rgb = self.f_color(latent_fg) # Mx3
		# put back the removed query points, for indices between idx[i] and idx[i+1], put fg_raw_rgb[i] at idx[i]
		fg_raw_rgb_full = torch.zeros((K-1)*P*D, 3, device=fg_raw_rgb.device, dtype=fg_raw_rgb.dtype) # ((K-1)xP*D)x3
		fg_raw_rgb_full[idx] = fg_raw_rgb
		fg_raw_rgb = fg_raw_rgb_full.view([K-1, P*D, 3])  # ((K-1)xP*D)x3 -> (K-1)x(P*D)x3

		fg_raw_shape = self.f_after_shape(tmp) # Mx1
		fg_raw_shape_full = torch.zeros((K-1)*P*D, 1, device=fg_raw_shape.device, dtype=fg_raw_shape.dtype) # ((K-1)xP*D)x1
		fg_raw_shape_full[idx] = fg_raw_shape
		fg_raw_shape = fg_raw_shape_full.view([K - 1, P*D])  # ((K-1)xP*D)x1 -> (K-1)x(P*D), density

		# if self.locality:
		# 	fg_raw_shape[outsider_idx] *= 0
		fg_raws = torch.cat([fg_raw_rgb, fg_raw_shape[..., None]], dim=-1)  # (K-1)x(P*D)x4

		all_raws = torch.cat([bg_raws, fg_raws], dim=0)  # Kx(P*D)x4
		raw_masks = F.relu(all_raws[:, :, -1:], True)  # Kx(P*D)x1
		masks = raw_masks / (raw_masks.sum(dim=0) + 1e-5)  # Kx(P*D)x1

		# print("ratio of fg density above 0.01", torch.sum(masks[1:] > 0.01) / idx.shape[0])
		# print("ratio of bg density above 0.01", torch.sum(masks[:1] > 0.01) / raw_masks[:1].numel())

		raw_rgb = (all_raws[:, :, :3].tanh() + 1) / 2
		raw_sigma = raw_masks + dens_noise * torch.randn_like(raw_masks)

		unmasked_raws = torch.cat([raw_rgb, raw_sigma], dim=2)  # Kx(P*D)x4
		masked_raws = unmasked_raws * masks
		raws = masked_raws.sum(dim=0)

		return raws, masked_raws, unmasked_raws, masks

# class SlotAttentionTFAnchor(nn.Module):
# 	def __init__(self, num_slots, in_dim=64, slot_dim=64, color_dim=8, iters=4, eps=1e-8, hidden_dim=128,
# 		  learnable_pos=True, n_feats=64*64, global_feat=False, feat_dropout_dim=None,
# 		  dropout=0., momentum=0.5, num_anchors=4, random_init_pos=False):
# 		super().__init__()
# 		self.num_slots = num_slots
# 		self.iters = iters
# 		self.eps = eps
# 		self.scale = slot_dim ** -0.5
# 		self.pos_momentum = momentum
# 		self.random_init_pos = random_init_pos
		
# 		if not self.random_init_pos:
# 			self.anchors = nn.Parameter(torch.rand(1, num_anchors, 2) * 1.5 - 0.75)
# 		self.slots_init_fg = nn.Parameter((torch.randn(1, num_anchors, slot_dim)))
# 		self.slots_init_bg = nn.Parameter((torch.randn(1, 1, slot_dim)))

# 		self.learnable_pos = learnable_pos

# 		if self.learnable_pos:
# 			self.attn_to_pos_bias = nn.Sequential(nn.Linear(n_feats, 2), nn.Tanh()) # range (-1, 1)
# 			self.attn_to_pos_bias[0].weight.data.zero_()
# 			self.attn_to_pos_bias[0].bias.data.zero_()

# 		self.to_kv = EncoderPosEmbedding(in_dim, slot_dim)
# 		self.to_q = nn.Sequential(nn.LayerNorm(slot_dim), nn.Linear(slot_dim, slot_dim, bias=False))
# 		self.to_q_bg = nn.Sequential(nn.LayerNorm(slot_dim), nn.Linear(slot_dim, slot_dim, bias=False))

# 		self.norm_feat = nn.LayerNorm(in_dim)
# 		if color_dim != 0:
# 			self.norm_feat_color = nn.LayerNorm(color_dim)
# 		self.slot_dim = slot_dim

# 		self.global_feat = global_feat

# 		self.dropout_shape_dim = feat_dropout_dim

# 		self.dropout = nn.Dropout(dropout)

# 	def forward(self, feat, feat_color=None, num_slots=None, dropout_shape_rate=None, 
# 			dropout_all_rate=None):
# 		"""
# 		input:
# 			feat: visual feature with position information, BxHxWxC
# 			feat_color: texture feature with position information, BxHxWxC'
# 			output: slots: BxKxC, attn: BxKxN
# 		"""
# 		B, H, W, _ = feat.shape
# 		N = H * W
# 		feat = feat.flatten(1, 2) # (B, N, C)

# 		K = num_slots if num_slots is not None else self.num_slots

# 		# random take num_slots anchors
# 		perm = torch.randperm(self.slots_init_fg.shape[1])[:K-1]
		
# 		if not self.random_init_pos:
# 			fg_position = self.anchors[:, perm].expand(B, -1, -1).to(feat.device) # (B, K-1, 2)
# 		else:
# 			fg_position = torch.rand(B, K-1, 2, device=feat.device) * 1.8 - 0.9 # (B, K-1, 2)
# 		slot_fg = self.slots_init_fg[:, perm].expand(B, -1, -1) # (B, K-1, C)
# 		slot_bg = self.slots_init_bg.expand(B, 1, -1) # (B, 1, C)
		
# 		feat = self.norm_feat(feat)
# 		if feat_color is not None:
# 			feat_color = self.norm_feat_color(feat_color)

# 		k_bg, v_bg = self.to_kv.forward_bg(feat, H, W) # (B,1,N,C)

# 		grid = build_grid(H, W, device=feat.device).flatten(1, 2) # (1,N,2)

# 		# attn = None
# 		for it in range(self.iters):
# 			fg_position_prev = fg_position
# 			slot_prev_bg = slot_bg
# 			slot_prev_fg = slot_fg
# 			q_fg = self.to_q(slot_fg)
# 			q_bg = self.to_q_bg(slot_bg) # (B,1,C)
			
# 			attn = torch.empty(B, K, N, device=feat.device)
			
# 			k, v = self.to_kv(feat, H, W, fg_position, 
# 			 			dropout_shape_dim=self.dropout_shape_dim, 
# 			 			dropout_shape_rate=dropout_shape_rate,
# 						dropout_all_rate=dropout_all_rate) # (B,K-1,N,C), (B,K-1,N,C)
			
# 			for i in range(K):
# 				if i != 0:
# 					k_i = k[:, i-1] # (B,N,C)
# 					slot_qi = q_fg[:, i-1] # (B,C)
# 					attn[:, i] = torch.einsum('bd,bnd->bn', slot_qi, k_i) * self.scale
# 				else:
# 					attn[:, i] = torch.einsum('bd,bnd->bn', q_bg.squeeze(1), k_bg.squeeze(1)) * self.scale
			
# 			attn = attn.softmax(dim=1) + self.eps  # BxKxN
# 			attn_fg, attn_bg = attn[:, 1:, :], attn[:, 0:1, :]  # Bx(K-1)xN, Bx1xN
# 			attn_weights_fg = attn_fg / attn_fg.sum(dim=-1, keepdim=True)  # Bx(K-1)xN
# 			attn_weights_bg = attn_bg / attn_bg.sum(dim=-1, keepdim=True)  # Bx1xN
			
# 			# momentum update slot position
# 			fg_position = torch.einsum('bkn,bnd->bkd', attn_weights_fg, grid) # (B,K-1,N) * (B,N,2) -> (B,K-1,2)
# 			fg_position = fg_position * (1 - self.pos_momentum) + fg_position_prev * self.pos_momentum

# 			if it != self.iters - 1:
# 				updates_fg = torch.empty(B, K-1, self.slot_dim, device=k.device) # (B,K-1,C)
# 				for i in range(K-1):
# 					v_i = v[:, i] # (B,N,C)
# 					attn_i = attn_weights_fg[:, i] # (B,N)
# 					updates_fg[:, i] = torch.einsum('bn,bnd->bd', attn_i, v_i)

# 				updates_bg = torch.einsum('bn,bnd->bd',attn_weights_bg.squeeze(1), v_bg.squeeze(1)) # (B,N,C) * (B,N) -> (B,C)
# 				updates_bg = updates_bg.unsqueeze(1) # (B,1,C)

# 				slot_bg = slot_prev_bg + self.dropout(updates_bg)
# 				slot_fg = slot_prev_fg + self.dropout(updates_fg)

# 			else:
# 				if self.learnable_pos: # add a bias term
# 					fg_position = fg_position + self.attn_to_pos_bias(attn_weights_fg) / 5 # (B,K-1,2)
# 					fg_position = fg_position.clamp(-1, 1) # (B,K-1,2)
					
# 				if feat_color is not None:
# 					# calculate slot color feature
# 					feat_color = feat_color.flatten(1, 2) # (B,N,C')
# 					if not self.global_feat:
# 						slot_fg_color = torch.einsum('bkn,bnd->bkd', attn_weights_fg, feat_color) # (B,K-1,N) * (B,N,C') -> (B,K-1,C')
# 						slot_bg_color = torch.einsum('bn,bnd->bd', attn_weights_bg.squeeze(1), feat_color).unsqueeze(1) # (B,N) * (B,N,C') -> (B,C'), (B,1,C')
# 					else:
# 						slot_fg_color = feat_color.repeat(1, K-1, 1) # (B,K-1,C')
# 						slot_bg_color = feat_color

# 		if feat_color is not None:
# 			slot_fg = torch.cat([slot_fg, slot_fg_color], dim=-1) # (B,K-1,C+C')
# 			slot_bg = torch.cat([slot_bg, slot_bg_color], dim=-1) # (B,1,C+C')
			
# 		slots = torch.cat([slot_bg, slot_fg], dim=1) # (B,K,C+C')
				
# 		return slots, attn, fg_position