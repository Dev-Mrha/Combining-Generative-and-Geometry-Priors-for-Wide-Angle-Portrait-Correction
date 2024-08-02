import torch
import torch.nn as nn
import cv2
import json
from tqdm import tqdm
import numpy as np
from model.combineNet import Model
from model.unet import U_Net_Line
import os
import torch.nn.functional as F
from PIL import Image
import torchvision.transforms as transforms
from torchvision.utils import save_image
import face_alignment
import dlib
import time

# device = "cuda" if torch.cuda.is_available() else "cpu"
device = 'cuda:0'
eps = 1e-6


def compute_cosin_similarity(preds, gts):  # shape
    people_num = min(preds.shape[0], gts.shape[0])
    points_num = gts.shape[1]
    similarity_list = []
    preds = preds.astype(np.float32)
    gts = gts.astype(np.float32)
    for people_index in range(people_num):
        # the index 63 of lmk is the center point of the face, that is, the tip of the nose
        pred_center = preds[people_index, 63, :]
        pred = preds[people_index, :, :]
        pred = pred - pred_center[None, :]
        gt_center = gts[people_index, 63, :]
        gt = gts[people_index, :, :]
        gt = gt - gt_center[None, :]

        dot = np.sum((pred * gt), axis=1)
        pred = np.sqrt(np.sum(pred * pred, axis=1))
        gt = np.sqrt(np.sum(gt * gt, axis=1))

        similarity_list_tmp = []
        for i in range(points_num):
            if i != 63:
                similarity = (dot[i] / (pred[i] * gt[i] + eps))
                similarity_list_tmp.append(similarity)

        similarity_list.append(np.mean(similarity_list_tmp))

    return np.mean(similarity_list)

def get_img_flow(model, img, rt=True):
    model.eval()
    with torch.no_grad():
        ret = model(img, return_flow=rt)
    return ret


def normalization(x):
    return [(float(i) - min(x)) / float(max(x) - min(x) + eps) for i in x]


def compute_line_slope_difference(pred_line, gt_k):  # line
    scores = []
    for i in range(pred_line.shape[0] - 1):
        pk = (pred_line[i + 1, 1] - pred_line[i, 1]) / (pred_line[i + 1, 0] - pred_line[i, 0] + eps)
        score = np.abs(pk - gt_k)
        scores.append(score)
    scores_norm = normalization(scores)
    score = np.mean(scores_norm)
    score = 1 - score
    return score


transform = transforms.Compose([
    transforms.Resize((384, 512)),
    transforms.ToTensor()])

tf_to = transforms.Compose([
    transforms.ToTensor()])

def landmark_loss(preds, gts):
    people_num = min(preds.shape[0], gts.shape[0])
    points_num = gts.shape[1]
    landmark_losses = []
    preds = preds.astype(np.float32)
    gts = gts.astype(np.float32)
    for people_index in range(people_num):  # people_num
        # the index 63 of lmk is the center point of the face, that is, the tip of the nose
        pred_center = preds[people_index, 63, :]
        pred = preds[people_index, :, :]
        pred = pred - pred_center[None, :]
        gt_center = gts[people_index, 63, :]
        gt = gts[people_index, :, :]
        gt = gt - gt_center[None, :]

        ldmk_loss = np.sqrt((pred - gt) ** 2)

        landmark_losses.append(ldmk_loss)

    return np.mean(landmark_losses)


def compute_ori2shape_face_line_metric(model, oriimg_paths):
    line_all_sum_pred = []
    face_all_sum_pred = []
    ldmk_loss_all = []
    oriimg_paths.sort()

    for oriimg_path in tqdm(oriimg_paths):
        # Get the [Source image]
        ori_img = Image.open(oriimg_path)  # Read the oriinal image
        input = ori_img.copy()  # # get the image as the input of our model

        # Get the landmarks from the [gt image]
        stereo_lmk_file = open(oriimg_path.replace(".jpg", "_stereo_landmark.json"))
        stereo_lmk = np.array(json.load(stereo_lmk_file), dtype="float32")

        # Get the landmarks from the [source image]
        ori_lmk_file = open(oriimg_path.replace(".jpg", "_landmark.json"))
        ori_lmk = np.array(json.load(ori_lmk_file), dtype="float32")

        out_lmk_file = open(oriimg_path.replace(".jpg", "_pred_mask_ldmk.json"))
        out_lmk = np.array(json.load(out_lmk_file), dtype="float32")

        stereo_lmk = sorted(stereo_lmk, key=lambda x: x[63][1])
        out_lmk = sorted(out_lmk, key=lambda x: x[63][1])
        ori_lmk = sorted(ori_lmk, key=lambda x: x[63][1])
        stereo_lmk = np.array(stereo_lmk)
        out_lmk = np.array(out_lmk)
        ori_lmk = np.array(ori_lmk)
        # Compute the face metric

        ori_width, ori_height = ori_img.size
        out_img, pred = get_img_flow(model, input)  # pred is flow_mid, only for lineAcc
        predflow_x, predflow_y = pred[:, :, 0], pred[:, :, 1]
        scale_x = ori_width / predflow_x.shape[1]
        scale_y = ori_height / predflow_x.shape[0]
        predflow_x = cv2.resize(predflow_x, (ori_width, ori_height)) * scale_x
        predflow_y = cv2.resize(predflow_y, (ori_width, ori_height)) * scale_y
        # Get the line from the [gt image]
        gt_line_file = oriimg_path.replace(".jpg", "_line_lines.json")
        lines = json.load(open(gt_line_file))

        # Get the line from the [source image]
        ori_line_file = oriimg_path.replace(".jpg", "_lines.json")
        ori_lines = json.load(open(ori_line_file))

        # Get the line from the pred out
        pred_ori2shape_lines = []
        for index, ori_line in enumerate(ori_lines):
            ori_line = np.array(ori_line, dtype="float32")
            pred_ori2shape = np.zeros_like(ori_line)
            for i in range(ori_line.shape[0]):
                x = ori_line[i, 0]
                y = ori_line[i, 1]
                pred_ori2shape[i, 0] = x - predflow_x[int(y), int(x)]
                pred_ori2shape[i, 1] = y - predflow_y[int(y), int(x)]
            pred_ori2shape = pred_ori2shape.tolist()
            pred_ori2shape_lines.append(pred_ori2shape)

        # Compute the lines score
        line_pred_ori2shape_sum = []
        for index, line in enumerate(lines):
            gt_line = np.array(line, dtype="float32")
            pred_ori2shape = np.array(pred_ori2shape_lines[index], dtype="float32")
            gt_k = (gt_line[1, 1] - gt_line[0, 1]) / (gt_line[1, 0] - gt_line[0, 0] + eps)
            pred_ori2shape_score = compute_line_slope_difference(pred_ori2shape, gt_k)
            line_pred_ori2shape_sum.append(pred_ori2shape_score)
        line_all_sum_pred.append(np.mean(line_pred_ori2shape_sum))

        face_pred_sim = compute_cosin_similarity(out_lmk, stereo_lmk)
        ldmk_loss = landmark_loss(out_lmk, stereo_lmk)
        face_all_sum_pred.append(face_pred_sim)
        ldmk_loss_all.append(ldmk_loss)
        stereo_lmk_file.close()
        ori_lmk_file.close()

    print(face_all_sum_pred)
    print(line_all_sum_pred)
    print(ldmk_loss_all)
    return np.mean(line_all_sum_pred) * 100, np.mean(face_all_sum_pred) * 100, np.mean(ldmk_loss_all)


def generate_out(model, img_pths):
    for oriimg_path in tqdm(img_pths):
        ori_img = Image.open(oriimg_path)  # Read the oriinal image
        input = ori_img.copy() 
        out_img = get_img_flow(model, input, False)  # pred is flow_mid, only for lineAcc
        cv2.imwrite(oriimg_path.replace(".jpg", "_pred.png"), out_img, [cv2.IMWRITE_JPEG_QUALITY, 90])


if __name__ == '__main__':
    print('using device ', device)
    mdl = Model(device).to(device)
    print('loading ckpts ...')
    mdl.load_ckpt('./pretrained_models/linenet.pt', './pretrained_models/e4e_best_model.pth', # linenet_1115_best
                  './pretrained_models/facenet.pt') # facenet_lq_best_model  iteration_175
    test_dir = "../test/"
    oriimg_paths = []
    for root, dirs, files in os.walk(test_dir):
        for file_name in files:
            if file_name.endswith(".jpg") or file_name.endswith(".png"):
                if "line" not in file_name and "stereo" not in file_name and "pred" not in file_name and \
                        "face" not in file_name and "mask" not in file_name and "ldmk" not in file_name and \
                        "output" not in file_name and "semi" not in file_name:
                    oriimg_paths.append(os.path.join(root, file_name))
    print("The number of images: :", len(oriimg_paths))
    oriimg_paths.sort()
    print('test begin')
    # print(oriimg_paths)
    # line_score, face_score, ldmk_loss = compute_ori2shape_face_line_metric(mdl, oriimg_paths)
    # print("Line_score = {:.4f}, Face_score = {:.4f}, ldmk_loss = {:.4f} ".format(line_score, face_score, ldmk_loss))
    generate_out(mdl, oriimg_paths)
