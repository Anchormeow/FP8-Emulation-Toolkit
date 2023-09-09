#------------------------------------------------------------------------------ 
# Copyright (c) 2023, Intel Corporation - All rights reserved. 
# This file is part of FP8-Emulation-Toolkit
#
# SPDX-License-Identifier: BSD-3-Clause
#------------------------------------------------------------------------------
# Naveen Mellempudi (Intel Corporation)
#------------------------------------------------------------------------------

from collections import OrderedDict
import torch
from .qutils import TensorQuantConfig, ModuleQuantConfig
from .qutils import get_or_update_model_quant_config_dict
from .qutils import reset_quantization_setup,add_quantization_hooks
from .qutils import quantize_model_weights,set_quantize_weights_flag
from .scale_shift import replace_batchnorms_with_scaleshifts
from .module_wrappers import BatchMatmul,Matmul,AddMatmul,EltwiseMul,EltwiseAdd,EltwiseDiv
from .module_wrappers import SparseConv2d,SparseLinear

'''
E4M3 Mixed Precision Emulator
'''
class E4M3Emulator(object):
    def __init__(self, model, optimizer, sparse_config=None, device="cuda", train=False, verbose=False, tensor_stats=False):
        super(E4M3Emulator, self).__init__()
        self.whitelist = [torch.nn.Conv2d, torch.nn.Linear, torch.nn.Embedding, torch.nn.EmbeddingBag]
        self.whitelist += [Matmul, BatchMatmul, AddMatmul]
        self.whitelist += [EltwiseAdd, EltwiseMul, EltwiseDiv]
        self.whitelist += [SparseConv2d, SparseLinear]
        self.blacklist = []
        self.list_unpatched = []
        self.is_training = train
        self.list_exempt_layers = None
        self.list_layers_output_fused = None
        self.device = device
        self.model = model
        self.patch_ops = False
        self.patch_impl = "NONE"
        self.patchlist = ["simple"]
        self.patchlist = ["SIMPLE"]
        self.sparse_config = sparse_config
        # default configuration
        self.data_emulation = True
        self.mod_qconfig = None
        self.model_qconfig_dict = None #OrderedDict()
        self.emb_qconfig    = TensorQuantConfig("e4m3", "rne")#, "per-channel")
        self.wt_qconfig     = TensorQuantConfig("e4m3", "rne")#, "per-channel")
        self.iact_qconfig   = TensorQuantConfig("e4m3", "rne")#, "per-tensor")
        self.oact_qconfig   = None #TensorQuantConfig("e4m3", "rne")
        self.hook_handles = None
        self.verbose = verbose

    def blacklist_modules(self, list_modules) :
        if self.verbose:
            print("Current module list {}".format(self.whitelist))
            print("Blacklist-ing modules {}".format(list_modules))
        for module in list_modules :
            self.blacklist.append(module)
            self.whitelist.remove(module)
        self.create_or_update_hooks()
        if self.verbose:
            print("Updated module list {}".format(self.whitelist))
            self.print_config()

    def whitelist_modules(self, list_modules) :
        if self.verbose:
            print("Current module list {}".format(self.whitelist))
            print("Whitelist-ing modules {}".format(list_modules))
        for module in list_modules :
            self.whitelist.append(module)
            self.blacklist.remove(module)
        self.create_or_update_hooks()
        if self.verbose:
            print("Updated module list {}".format(self.whitelist))
            self.print_config()

    def create_or_update_hooks(self, model):
        self.model_qconfig_dict = get_or_update_model_quant_config_dict(model,
                                    self.whitelist, self.mod_qconfig,
                                    model_qconfig_dict=self.model_qconfig_dict,
                                    override=True)

        if self.list_exempt_layers is not None :
            for exempt_layer in self.list_exempt_layers:
                if self.model_qconfig_dict.get(exempt_layer) is not None: #如果有值
                    self.model_qconfig_dict.pop(exempt_layer) #删除键值

		# Disable output quantization for these layers,
		# These layers are followed by precision sensitive layers such as SoftMax
        # In the final implementation, sensitive layers are fused with the preceeding layer
        if self.list_layers_output_fused is not None :
            for name,module in model.named_modules():
                if name in self.list_layers_output_fused and name not in self.list_exempt_layers \
                and module in self.whitelist :
                    self.model_qconfig_dict[name].oact_qconfig = None
                    self.model_qconfig_dict[name].ograd_qconfig = None

        # additional handling of HW patching 
        for name,module in model.named_modules():
            if type(module) in [torch.nn.Conv2d] and name in self.model_qconfig_dict:
                if module.in_channels < 64 or module.out_channels < 64:
                    self.model_qconfig_dict[name].patch_ops = False
                    self.model_qconfig_dict[name].patch_impl = "NONE"
                    self.list_unpatched += [name]

        # Except for Conv2d, and Linear module disable quantization on weight
        for name,module in model.named_modules():
            if type(module) not in [torch.nn.Conv2d, torch.nn.Linear]\
                and name in self.model_qconfig_dict:
                self.model_qconfig_dict[name].wt_qconfig = None
                self.model_qconfig_dict[name].wtgrad_qconfig = None

        for name,module in model.named_modules():
            if ((type(module) == torch.nn.Embedding) or (type(module) == torch.nn.EmbeddingBag))\
                and name in self.model_qconfig_dict:
                self.model_qconfig_dict[name].wt_qconfig = self.emb_qconfig
                self.model_qconfig_dict[name].iact_qconfig = None
                self.model_qconfig_dict[name].igrad_qconfig = None
                self.model_qconfig_dict[name].ograd_qconfig = None
                self.model_qconfig_dict[name].oact_qconfig = None

        for name,module in model.named_modules():
            if type(module) in [BatchMatmul]\
                and name in self.model_qconfig_dict:
                self.model_qconfig_dict[name].wt_qconfig = None
                self.model_qconfig_dict[name].wtgrad_qconfig = None
                self.model_qconfig_dict[name].oact_qconfig = None
                self.model_qconfig_dict[name].ograd_qconfig = None

        reset_quantization_setup(model, self.model_qconfig_dict)
        # Adding hooks for quantizing input.
        self.hook_handles = add_quantization_hooks(model, self.model_qconfig_dict, is_training=self.is_training)
        if not self.is_training : #此时train为false
            print("e4m3 : quantizing model weights..")
            quantize_model_weights(model, self.model_qconfig_dict)
            set_quantize_weights_flag(model, self.model_qconfig_dict, False)

    def prepare_model(self, model, list_exempt_layers, list_layers_output_fused):
        mod_qconfig = ModuleQuantConfig(wt_qconfig=self.wt_qconfig,
                                    iact_qconfig=self.iact_qconfig,
                                    oact_qconfig=self.oact_qconfig,
				    patch_ops=self.patch_ops)
        mod_qconfig.device = self.device
        mod_qconfig.patch_impl = self.patch_impl
        mod_qconfig.sparse_config = self.sparse_config
        self.mod_qconfig = mod_qconfig
        self.list_exempt_layers = list_exempt_layers
        self.list_layers_output_fused = list_layers_output_fused
        self.create_or_update_hooks(model)

    def enable_hw_patching(self, patch_ops):
        if patch_ops != 'NONE':
            if patch_ops in self.patchlist :
                self.patch_ops = True
                self.patch_impl = patch_ops
                print("e4m3_emulator: PyTorch Ops are monkey-patched to use {} kernels : {}".format(self.patch_impl, self.patch_ops))
            else :
                raise RuntimeError("e4m3_emulator: HW patching is not supported for {}, supported list of options : {}".format(patch_ops, self.patchlist))

    def fuse_batchnorm_with_convolution(self, model):
        from torch.nn.utils.fusion import fuse_conv_bn_eval
        temp = []
        for name, module in model.named_children():
            if list(module.named_children()):
                self.fuse_batchnorm_with_convolution(module)

            if isinstance(module, torch.nn.BatchNorm2d):
                if isinstance(temp[-1][1], torch.nn.Conv2d):
                    setattr(model, temp[-1][0], fuse_conv_bn_eval(temp[-1][1], module))
                    setattr(model, name, torch.nn.Identity())
            else:
                temp.append((name, module))
        return model

    def set_calibration_qconfig(self):
        self.emb_qconfig    = TensorQuantConfig("e4m3", "rne", "per-channel")
        self.wt_qconfig     = TensorQuantConfig("e4m3", "rne", "per-channel")
        self.iact_qconfig   = TensorQuantConfig("e4m3", "rne", "per-tensor")
        self.oact_qconfig   = None 

    def set_default_inference_qconfig(self):
        self.emb_qconfig    = TensorQuantConfig("e4m3", "rne", "per-channel")
        self.wt_qconfig     = TensorQuantConfig("e4m3", "rne", "per-channel")
        self.iact_qconfig   = TensorQuantConfig("e4m3", "rne", "per-tensor")
        self.oact_qconfig   = None 

    def fuse_layers_and_quantize_model(self, model):
        if self.is_training :
            print("Warning : emulator.is_training is set to True, returning the model unchanged")
            return model
        if self.verbose :
            print("Fusing Batchnorm layers and replacing them with scale and shift")

        model = replace_batchnorms_with_scaleshifts(model)
        #model = self.fuse_batchnorm_with_convolution(model)
        self.is_training = False
        self.set_default_inference_qconfig()
        self.prepare_model(model, self.list_exempt_layers, self.list_layers_output_fused)
        #reset_quantization_setup(model, self.model_qconfig_dict)
        #add_quantization_hooks(model, self.model_qconfig_dict)
        #quantize_model_weights(model, self.model_qconfig_dict) # added new
        #set_quantize_weights_flag(model, self.model_qconfig_dict, False)
        model = model.to(self.device)
        if self.verbose :
            self.print_config()

        return model

    def print_config(self):
        for key in self.model_qconfig_dict:
            print("{} {:40s}".format(self.model_qconfig_dict[key], key))

    def __repr__(self):
        train_infer = "inference"
        if self.is_training :
            train_infer = "training"
        return "[Configured to run {} on {}, using AMP: {}]".format(str(train_infer), self.device, str(self.using_apex))
