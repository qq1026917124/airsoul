#!/usr/bin/env python
# coding=utf8
# File: models.py
import sys
import random
import torch
import numpy
from torch import nn
from torch.nn import functional as F
from torch.utils.checkpoint import checkpoint  
from l3c_baselines.utils import weighted_loss, img_pro, img_post
from l3c_baselines.utils import parameters_regularization, count_parameters
from l3c_baselines.utils import log_debug, log_warn, log_fatal
from l3c_baselines.modules import ImageEncoder, ImageDecoder
from .decision_model import RSADecisionModel

class AnyMDPRSA(RSADecisionModel):
    def __init__(self, config, verbose=False): 
        super().__init__(config)

        # Loss weighting
        loss_weight = torch.cat( (torch.linspace(1.0e-3, 1.0, config.context_warmup),
                                  torch.full((config.max_position_loss_weighting - config.context_warmup,), 1.0)
                                  ), 
                                dim=0)
        loss_weight = loss_weight / torch.sum(loss_weight)

        self.register_buffer('loss_weight', loss_weight)
        self.set_train_config(config)

        self.nactions = config.action_dim

        if(verbose):
            log_debug("RSA Decision Model initialized, total params: {}".format(count_parameters(self)))
            log_debug("Causal Block Parameters： {}".format(count_parameters(self.causal_model)))

    def set_train_config(self, config):
        if(config.has_attr("frozen_modules")):
            if("causal_model" in config.frozen_modules):
               self.causal_model.requires_grad_(False)
            else:
               self.causal_model.requires_grad_(True)

            if("s_encoder" in config.frozen_modules):
                self.s_encoder.requires_grad_(False)
            else:
                self.s_encoder.requires_grad_(True)

            if("a_encoder" in config.frozen_modules):
                self.a_encoder.requires_grad_(False)
            else:
                self.a_encoder.requires_grad_(True)

            if(self.r_included):
                if("r_encoder" in config.frozen_modules):
                    self.r_encoder.requires_grad_(False)
                else:
                    self.r_encoder.requires_grad_(True)

            if(self.p_included):
                if("p_encoder" in config.frozen_modules):
                    self.p_encoder.requires_grad_(False)
                else:
                    self.p_encoder.requires_grad_(True)

            if("s_decoder" in config.frozen_modules):
                self.s_decoder.requires_grad_(False)
            else:
                self.s_decoder.requires_grad_(True)

            if("a_decoder" in config.frozen_modules):
                self.a_decoder.requires_grad_(False)
            else:
                self.a_decoder.requires_grad_(True)

            if("r_decoder" in config.frozen_modules):
                self.r_decoder.requires_grad_(False)
            else:
                self.r_decoder.requires_grad_(True)

    def sequential_loss(self, prompts, 
                            observations, 
                            rewards, 
                            behavior_actions, 
                            label_actions, 
                            state_dropout=0.0,
                            reward_dropout=0.0,
                            update_memory=True,
                            use_loss_weight=True,
                            reduce_dim=1):
    
        bsz = behavior_actions.shape[0]
        seq_len = behavior_actions.shape[1]
        # Pay attention position must be acquired before calling forward()
        ps = self.causal_model.position
        pe = ps + seq_len

        # Predict the latent representation of action and next frame (World Model)
        s_pred, a_pred, r_pred, _ = self.forward(
                prompts, observations[:, :-1], behavior_actions, rewards,
                cache=None, need_cache=False, state_dropout=state_dropout,
                update_memory=update_memory)

        # Calculate the loss information
        loss = dict()

        # Mask out the invalid actions
        loss_weight = (label_actions.ge(0) * label_actions.lt(self.nactions)).to(self.loss_weight.dtype)
        if(use_loss_weight):
            loss_weight = loss_weight * self.loss_weight[ps:pe].unsqueeze(0)

        # World Model Loss - States and Rewards
        loss["wm-s"], loss["count"] = weighted_loss(s_pred, 
                                     gt=observations[:, 1:], 
                                     loss_type="ce",
                                     loss_wht=loss_weight, 
                                     reduce_dim=reduce_dim,
                                     need_cnt=True)
        loss["wm-r"] = weighted_loss(r_pred, 
                                     gt=rewards.view(*rewards.shape,1), 
                                     loss_type="mse",
                                     loss_wht=loss_weight, 
                                     reduce_dim=reduce_dim)

        # Policy Model
        loss["pm"] = weighted_loss(a_pred, 
                                   gt=label_actions, 
                                   loss_type="ce",
                                   loss_wht=loss_weight, 
                                   reduce_dim=reduce_dim)
        # Entropy Loss
        loss["ent"] = weighted_loss(a_pred, 
                                    loss_type="ent", 
                                    loss_wht=loss_weight,
                                    reduce_dim=reduce_dim)

        loss["causal-l2"] = parameters_regularization(self)

        return loss
    
    def generate(self, prompts,
                       observation,
                       temp,
                       action_clip=None,
                       need_numpy=True,
                       single_batch=True):
        """
        Generating Step By Step Action and Next Frame Prediction
        Args:
            prompts: single prompt of size 
            observation: [bsz, 1]
            temp: temperature for sampling
            device: cuda or cpu
        Returns:
            o_pred: predicted states
            action_out: action decision
            r_pred: predicted rewards
        """
        device = self.parameters()[0].device
        if(not self.p_included):
            pro_in = None
        elif(not isinstance(prompts, torch.Tensor)):
            pro_in = torch.tensor([prompts], dtype=torch.int64).to(device)
        else:
            pro_in = prompts
        if(not isinstance(observation, torch.Tensor)):
            obs_in = torch.tensor([observation], dtype=torch.int64).to(device)
        if(single_batch):
            if(pro_in is not None):
                pro_in.unsqueeze(0)
            obs_in.unsqueeze(0)

        if(self.r_included):
            default_r = self.default_r
        else:
            default_r = None

        o_pred, a_pred, r_pred, _ = self.forward(
            pro_in,
            obs_in,
            self.default_a,
            default_r,
            T=temp,
            update_memory=False,
            need_cache=False)
        
        if(self.a_is_discrete):
            if(action_clip is not None):
                a_pred[:, :, action_clip:] = 0.0
            act_in = a_pred / a_pred.sum(dim=-1, keepdim=True)
            # bsz, 1, nactions
            act_in = torch.multinomial(act_in.squeeze(1), num_samples=1).squeeze(1)
            act_out = act_in.squeeze()
        
        o_pred, a_pred, r_pred, _ = self.forward(
            prompts,
            obs_in,
            act_in,
            default_r,
            T=temp,
            update_memory=False,
            need_cache=False)
        
        act_out = act_out.detach().cpu().squeeze()
        state = o_pred.detach().cpu().squeeze()
        reward = r_pred.detach().cpu().squeeze()

        if(need_numpy):
            act_out = act_out.numpy()
            state = state.numpy()
            reward = reward.numpy()
        
        return state, act_out, reward

    def in_context_learn(self, prompts,
                    observation,
                    action,
                    reward,
                    cache=None,
                    need_cache=False,
                    single_batch=True,
                    single_step=True):
        """
        In Context Reinforcement Learning Through an Sequence of Steps
        """
        device = self.parameters()[0].device
        pro_in = None
        obs_in = None

        if(single_batch and single_step):
            extra_dim = (0, 1)
        elif(single_batch):
            extra_dim = (0,)
        elif(single_step):
            extra_dim = (1,)
        else:
            extra_dim = None

        if(prompts is not None and not isinstance(prompts, torch.Tensor)):
            pro_in = torch.tensor([prompts], dtype=torch.int64).to(device)
            if(extra_dim is not None):
                pro_in = pro_in.unsqueeze(*extra_dim)
        if(not isinstance(observation, torch.Tensor)):
            obs_in = torch.tensor([observation], dtype=torch.int64).to(device)
            if(extra_dim is not None):
                obs_in = obs_in.unsqueeze(*extra_dim)
        if(not isinstance(action, torch.Tensor)):
            act_in = torch.tensor([action], dtype=torch.int64).to(device)
            if(extra_dim is not None):
                act_in = act_in.unsqueeze(*extra_dim)
        if(not isinstance(reward, torch.Tensor) and reward is None):
            rew_in = torch.tensor([reward], dtype=torch.int64).to(device)
            if(extra_dim is not None):
                rew_in = rew_in.unsqueeze(*extra_dim)

        # s, a, r = obs, act_pred, r_pred; update memory = true
        _, _, _, new_cache = self.forward(
            pro_in,
            obs_in,
            act_in,
            rew_in,
            need_cache=need_cache
            update_memory=True)
        
        return new_cache

        

if __name__=="__main__":
    from utils import Configure
    config=Configure()
    config.from_yaml(sys.argv[1])

    model = AnyMDPRSA(config.model_config)

    observation = torch.randn(8, 33, 3, 128, 128)
    action = torch.randint(4, (8, 32)) 
    reward = torch.randn(8, 32)
    local_map = torch.randn(8, 32, 3, 7, 7)

    vae_loss = model.vae_loss(observation)
    losses = model.sequential_loss(None, observation, reward, action, action)
    rec_img, img_out, act_out, cache = model.inference_step_by_step(
            observation[:, :5], action[:, :4], 1.0, 0, observation.device)
    print("vae:", vae_loss, "sequential:", losses)
    print(img_out[0].shape, act_out.shape)
    print(len(cache))
    print(cache[0].shape)
