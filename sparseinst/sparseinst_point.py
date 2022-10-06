# Copyright (c) Tianheng Cheng and its affiliates. All Rights Reserved

import torch
import torch.nn as nn
import torch.nn.functional as F

from detectron2.modeling import build_backbone
from detectron2.structures import ImageList, Instances, BitMasks
from detectron2.modeling import META_ARCH_REGISTRY, build_backbone
from skimage import color

from .encoder import build_sparse_inst_encoder
from .decoder import build_sparse_inst_decoder
from .loss import build_sparse_inst_criterion
from .utils import nested_tensor_from_tensor_list

__all__ = ["SparseInst"]


def unfold_wo_center(x, kernel_size, dilation):
    assert x.dim() == 4
    assert kernel_size % 2 == 1

    # using SAME padding
    padding = (kernel_size + (dilation - 1) * (kernel_size - 1)) // 2
    unfolded_x = F.unfold(
        x, kernel_size=kernel_size,
        padding=padding,
        dilation=dilation
    )

    unfolded_x = unfolded_x.reshape(
        x.size(0), x.size(1), -1, x.size(2), x.size(3)
    )

    # remove the center pixels
    size = kernel_size ** 2
    unfolded_x = torch.cat((
        unfolded_x[:, :, :size // 2],
        unfolded_x[:, :, size // 2 + 1:]
    ), dim=2)

    return unfolded_x


def get_images_color_similarity(images, image_masks, kernel_size, dilation):
    assert images.dim() == 4
    assert images.size(0) == 1

    unfolded_images = unfold_wo_center(
        images, kernel_size=kernel_size, dilation=dilation
    )

    diff = images[:, :, None] - unfolded_images
    similarity = torch.exp(-torch.norm(diff, dim=1) * 0.5)

    unfolded_weights = unfold_wo_center(
        image_masks[None, None], kernel_size=kernel_size,
        dilation=dilation
    )[:,0]
    # unfolded_weights = torch.max(unfolded_weights, dim=1)[0]

    return similarity * unfolded_weights

@torch.jit.script
def rescoring_mask(scores, mask_pred, masks):
    mask_pred_ = mask_pred.float()
    return scores * ((masks * mask_pred_).sum([1, 2]) / (mask_pred_.sum([1, 2]) + 1e-6))


@META_ARCH_REGISTRY.register()
class SparseInst(nn.Module):

    def __init__(self, cfg):
        super().__init__()

        # move to target device
        self.device = torch.device(cfg.MODEL.DEVICE)

        # backbone
        self.backbone = build_backbone(cfg)
        self.size_divisibility = self.backbone.size_divisibility
        output_shape = self.backbone.output_shape()

        # encoder & decoder
        self.encoder = build_sparse_inst_encoder(cfg, output_shape)
        self.decoder = build_sparse_inst_decoder(cfg)

        # matcher & loss (matcher is built in loss)
        self.criterion = build_sparse_inst_criterion(cfg)
        # self.register_buffer("_iter", torch.zeros([1]))

        # data and preprocessing
        self.mask_format = cfg.INPUT.MASK_FORMAT

        self.pixel_mean = torch.Tensor(
            cfg.MODEL.PIXEL_MEAN).to(self.device).view(3, 1, 1)
        self.pixel_std = torch.Tensor(
            cfg.MODEL.PIXEL_STD).to(self.device).view(3, 1, 1)
        # self.normalizer = lambda x: (x - pixel_mean) / pixel_std

        # inference
        self.cls_threshold = cfg.MODEL.SPARSE_INST.CLS_THRESHOLD
        self.mask_threshold = cfg.MODEL.SPARSE_INST.MASK_THRESHOLD
        self.max_detections = cfg.MODEL.SPARSE_INST.MAX_DETECTIONS

        # for pairwise loss
        self.pairwise_size = 3
        self.pairwise_dilation = 2

    def normalizer(self, image):
        image = (image - self.pixel_mean) / self.pixel_std
        return image

    def preprocess_inputs(self, batched_inputs):
        images = [x["image"].to(self.device) for x in batched_inputs]
        # original_image_masks = [torch.ones_like(x[0], dtype=torch.float32) for x in images]
        images = [self.normalizer(x) for x in images]
        images = ImageList.from_tensors(images, 32)
        # original_image_masks = ImageList.from_tensors(
        #     original_image_masks, 32)
        return images

    def prepare_targets(self, targets):
        new_targets = []
        for targets_per_image in targets:
            target = {}
            gt_classes = targets_per_image.gt_classes
            gt_color_similarity = targets_per_image.image_color_similarity
            target["color_sim"] = gt_color_similarity.to(self.device)
            target["labels"] = gt_classes.to(self.device)
            h, w = targets_per_image.image_size
            # if not targets_per_image.has('gt_masks'):
            #     gt_masks = BitMasks(torch.empty(0, h, w))
            # else:
            #     gt_masks = targets_per_image.gt_masks
            #     if self.mask_format == "polygon":
            #         if len(gt_masks.polygons) == 0:
            #             gt_masks = BitMasks(torch.empty(0, h, w))
            #         else:
            #             gt_masks = BitMasks.from_polygon_masks(
            #                 gt_masks.polygons, h, w)
            gt_box = targets_per_image.gt_boxes
            target["boxes"] = gt_box.to(self.device)
            per_im_boxes = gt_box
            per_im_bitmasks_full = []
            for per_box in per_im_boxes:
                bitmask_full = torch.zeros((h, w), device=self.device).float()
                bitmask_full[int(per_box[1]):int(per_box[3] + 1), int(per_box[0]):int(per_box[2] + 1)] = 1.0

                per_im_bitmasks_full.append(bitmask_full)  # 由bbox生成伪mask

            gt_masks = torch.stack(per_im_bitmasks_full, dim=0)
            target["masks"] = BitMasks(gt_masks)
            new_targets.append(target)

        return new_targets


    def prepare_targets(self, targets):
        new_targets = []
        for targets_per_image in targets:
            target = {}
            gt_classes = targets_per_image.gt_classes
            gt_color_similarity = targets_per_image.image_color_similarity
            target["color_sim"] = gt_color_similarity.to(self.device)
            target["labels"] = gt_classes.to(self.device)
            h, w = targets_per_image.image_size
            # if not targets_per_image.has('gt_masks'):
            #     gt_masks = BitMasks(torch.empty(0, h, w))
            # else:
            #     gt_masks = targets_per_image.gt_masks
            #     if self.mask_format == "polygon":
            #         if len(gt_masks.polygons) == 0:
            #             gt_masks = BitMasks(torch.empty(0, h, w))
            #         else:
            #             gt_masks = BitMasks.from_polygon_masks(
            #                 gt_masks.polygons, h, w)
            gt_box = targets_per_image.gt_boxes
            target["boxes"] = gt_box.to(self.device)
            per_im_boxes = gt_box
            per_im_bitmasks_full = []
            for per_box in per_im_boxes:
                bitmask_full = torch.zeros((h, w), device=self.device).float()
                bitmask_full[int(per_box[1]):int(per_box[3] + 1), int(per_box[0]):int(per_box[2] + 1)] = 1.0

                per_im_bitmasks_full.append(bitmask_full)  # 由bbox生成伪mask

            gt_masks = torch.stack(per_im_bitmasks_full, dim=0)
            target["masks"] = BitMasks(gt_masks)
            new_targets.append(target)

        return new_targets

    def forward(self, batched_inputs):
        images = self.preprocess_inputs(batched_inputs)
        if isinstance(images, (list, torch.Tensor)):
            images = nested_tensor_from_tensor_list(images)
        max_shape = images.tensor.shape[2:]
        # forward
        features = self.backbone(images.tensor)
        features = self.encoder(features)
        output = self.decoder(features)

        if self.training:
            gt_instances = [x["instances"].to(
                self.device) for x in batched_inputs]
            original_images = [x["image"].to(self.device) for x in batched_inputs]
            original_images = ImageList.from_tensors(original_images, 32)
            original_image_masks = [torch.ones_like(x[0], dtype=torch.float32) for x in original_images]
            original_image_masks = ImageList.from_tensors(
                original_image_masks, 32)
            self.add_bitmasks_from_boxes(
                gt_instances, original_images.tensor, original_image_masks.tensor,
                original_images.tensor.size(-2), original_images.tensor.size(-1)
            )
            targets = self.prepare_targets(gt_instances)
            losses = self.criterion(output, targets, max_shape)
            return losses
        else:
            results = self.inference(
                output, batched_inputs, max_shape, images.image_sizes)
            processed_results = [{"instances": r} for r in results]
            return processed_results

    def forward_test(self, images):
        # for inference, onnx, tensorrt
        # input images: BxCxHxW, fixed, need padding size
        # normalize
        images = (images - self.pixel_mean[None]) / self.pixel_std[None]
        features = self.backbone(images)
        features = self.encoder(features)
        output = self.decoder(features)

        pred_scores = output["pred_logits"].sigmoid()
        pred_masks = output["pred_masks"].sigmoid()
        pred_objectness = output["pred_scores"].sigmoid()
        pred_scores = torch.sqrt(pred_scores * pred_objectness)
        pred_masks = F.interpolate(
            pred_masks, scale_factor=4.0, mode="bilinear", align_corners=False)
        return pred_scores, pred_masks

    def inference(self, output, batched_inputs, max_shape, image_sizes):
        # max_detections = self.max_detections
        results = []
        pred_scores = output["pred_logits"].sigmoid()
        pred_masks = output["pred_masks"].sigmoid()
        pred_objectness = output["pred_scores"].sigmoid()
        pred_scores = torch.sqrt(pred_scores * pred_objectness)

        for _, (scores_per_image, mask_pred_per_image, batched_input, img_shape) in enumerate(zip(
                pred_scores, pred_masks, batched_inputs, image_sizes)):

            ori_shape = (batched_input["height"], batched_input["width"])
            result = Instances(ori_shape)
            # max/argmax
            scores, labels = scores_per_image.max(dim=-1)
            # cls threshold
            keep = scores > self.cls_threshold
            scores = scores[keep]
            labels = labels[keep]
            mask_pred_per_image = mask_pred_per_image[keep]

            if scores.size(0) == 0:
                result.scores = scores
                result.pred_classes = labels
                results.append(result)
                continue

            h, w = img_shape
            # rescoring mask using maskness
            scores = rescoring_mask(
                scores, mask_pred_per_image > self.mask_threshold, mask_pred_per_image)

            # upsample the masks to the original resolution:
            # (1) upsampling the masks to the padded inputs, remove the padding area
            # (2) upsampling/downsampling the masks to the original sizes
            mask_pred_per_image = F.interpolate(
                mask_pred_per_image.unsqueeze(1), size=max_shape, mode="bilinear", align_corners=False)[:, :, :h, :w]
            mask_pred_per_image = F.interpolate(
                mask_pred_per_image, size=ori_shape, mode='bilinear', align_corners=False).squeeze(1)

            mask_pred = mask_pred_per_image > self.mask_threshold
            # fix the bug for visualization
            # mask_pred = BitMasks(mask_pred)

            # using Detectron2 Instances to store the final results
            result.pred_masks = mask_pred
            result.scores = scores
            result.pred_classes = labels
            results.append(result)

        return results

    def add_bitmasks_from_boxes(self, instances, images, image_masks, im_h, im_w):
        stride = 4
        start = int(stride // 2)

        assert images.size(2) % stride == 0
        assert images.size(3) % stride == 0

        downsampled_images = F.avg_pool2d(
            images.float(), kernel_size=stride,
            stride=stride, padding=0
        )
        image_masks = image_masks[:, start::stride, start::stride]

        for im_i, per_im_gt_inst in enumerate(instances):
            images_lab = color.rgb2lab(downsampled_images[im_i].byte().permute(1, 2, 0).cpu().numpy())  # 图像色彩空间变换
            images_lab = torch.as_tensor(images_lab, device=downsampled_images.device, dtype=torch.float32)
            images_lab = images_lab.permute(2, 0, 1)[None]
            images_color_similarity = get_images_color_similarity(
                images_lab, image_masks[im_i],
                self.pairwise_size, self.pairwise_dilation
            )  # BoxInst

            if len(per_im_gt_inst) > 0:
                per_im_gt_inst.image_color_similarity = torch.cat([
                    images_color_similarity for _ in range(len(per_im_gt_inst))
                ], dim=0)  # color sim

