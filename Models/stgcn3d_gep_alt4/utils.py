import torch
import torch.nn as nn
import torch.nn.functional as F

import numpy as np
from sklearn.cluster import KMeans

import collections

import os
import json

import sys
sys.path.append(os.path.join(os.getcwd(), '..', 'grammar', 'src'))
from grammarutils import *
from GEP import GeneralizedEarley as GEP

class WriteOnceDict(dict):
	def __setitem__(self, key, value):
		if not key in self:
			super(WriteOnceDict, self).__setitem__(key, value)


def data_feeder(batch_data):
    N, T, V, _ = batch_data.size()
    data = torch.zeros(N, T, V, V, 4)
    for i in range(V):
        for j in range(V):
            data[:, :, i, j, :2] = batch_data[:, :, i, :2]
            data[:, :, i, j, 2:] = batch_data[:, :, j, :2]
    data = data.permute(0, 4, 1, 2, 3).contiguous()

    return data


def data_feeder_onehots(batch_onehots, num_node_list, vmax):
    N, T, C = batch_onehots.size()
    data = torch.zeros(N, T, vmax, C)
    for num in range(N):
        for i in range(T):
            data[num, i, :num_node_list[num]] = batch_onehots[num, i]
    data = data.permute(0, 3, 1, 2).contiguous()

    return data


def data_feeder_gae(batch_data):
    N, T, V, _ = batch_data.size()
    data = torch.zeros(N, T, V, V, 4).to(batch_data)
    for i in range(V):
        for j in range(V):
            data[:, :, i, j, :2] = batch_data[:, :, i, :2]
            data[:, :, i, j, 2:] = batch_data[:, :, j, :2]
    data = data.permute(0, 1, 4, 2, 3).contiguous()
    data = data.permute(1, 0, 2, 3, 4).contiguous()

    return data


def data_batch(input_data_list, pred_data_list, num_node_list, obs_len, pred_len, num_feature, vmax):
    N = len(input_data_list)

    batch_input_data = torch.zeros(N, obs_len, vmax, num_feature)
    for i, input_data in enumerate(input_data_list):
        batch_input_data[i, :, :num_node_list[i], :] = input_data

    batch_pred_data = torch.zeros(N, pred_len, vmax, num_feature)
    for i, pred_data in enumerate(pred_data_list):
        batch_pred_data[i, :, :num_node_list[i], :] = pred_data

    batch_data = torch.cat((batch_input_data, batch_pred_data), dim=1)

    return batch_data


def data_vectorize(batch_data_seq):
    batch_vectorized_seq = []
    first_value_dicts = []
    for data_seq in batch_data_seq:
        first_value_dict = WriteOnceDict()
        vectorized_seq = []

        num_nodes = data_seq.size(1)
        frame0 = data_seq[0, :]
        for node in range(num_nodes):
            first_value_dict[node] = frame0[node, :]
        for i in range(1, len(data_seq)):
            frame = data_seq[i]
            vectorized_frame = torch.zeros(num_nodes, data_seq.size(-1))
            for node in range(num_nodes):
                vectorized_frame[node] = frame[node, :]-first_value_dict[node]
            vectorized_seq.append(vectorized_frame)
        batch_vectorized_seq.append(torch.stack(vectorized_seq))
        first_value_dicts.append(first_value_dict)

    return torch.stack(batch_vectorized_seq), first_value_dicts


def data_revert(batch_data_seq, first_value_dicts):
    batch_reverted_seq = []

    for i in range(len(batch_data_seq)):
        data_seq = batch_data_seq[i]
        first_value_dict = first_value_dicts[i]
        reverted_seq = data_seq.clone()

        num_nodes = data_seq.size(1)
        for j, frame in enumerate(data_seq):
            for node in range(num_nodes):
                reverted_seq[j, node, :2] = frame[node, :2]+first_value_dict[node][:2].to(frame)
        batch_reverted_seq.append(reverted_seq)

    return torch.stack(batch_reverted_seq)


def cluster_prob(batch_feature, cluster_obj, nc, eps=1e-6):
    cluster_cs = cluster_obj.cluster_centers_

    batch_prob = []
    for feature in batch_feature:
        dists = np.linalg.norm(cluster_cs-feature, axis=1)
        inv_dists = 1.0/(dists+eps)
        # prob = np.exp(1.0/(dists+eps))/np.sum(np.exp(1.0/(dists+eps)))
        prob = inv_dists/np.sum(inv_dists)
        batch_prob.append(prob)
    
    return np.stack(batch_prob)


def convert_one_hots(sentence, nc):
    seq_len, N = sentence.size()
    one_hots = torch.zeros(N, seq_len, nc)

    for num in range(N):
        for i in range(seq_len):
            one_hots[num, i, sentence[i, num].item()] = 1.0

    return one_hots


def gep_init(args):
    grammar_file = os.path.join(args.grammar_root, args.grammar_file)
    duration_file = os.path.join(args.grammar_root, args.grammar_prior)

    symbols = [str(num) for num in range(args.nc)]
    symbol_index = dict()
    for s in symbols:
        symbol_index[s] = symbols.index(s)
    grammar = read_grammar(grammar_file, index=True, mapping=symbol_index)
    parser = GEP(grammar)

    with open(duration_file, 'r') as f:
        duration_prior = json.load(f)

    return parser, duration_prior


def gep_convert_sentence(sentence_prob, grammar_gep):
    seq_len, N, k = sentence_prob.size()
    sentence_prob_np = sentence_prob.data.numpy()

    parsed_sentence = torch.zeros(seq_len, N, dtype=torch.long)

    for num in range(N):
        parsed_sentence[0, num] = torch.argmax(sentence_prob[0, num])
    for i in range(1, seq_len):
        for num in range(N):
            sentence_seq = gep_parse(sentence_prob_np[:i, num, :], sentence_prob_np[i, num, :], grammar_gep)
            parsed_sentence[i, num] = sentence_seq[-1]

    return parsed_sentence


def gep_pred_parse(batch_obs_sentence, pred_len, duration_prior, parser, args):
    obs_len, N, k = batch_obs_sentence.shape
    batch_predictions = []

    for num in range(N):
        obs_sentence = batch_obs_sentence[:, num, :]
        predictions = np.zeros((pred_len, k), dtype=np.float32)

        best_parse, prob = parser.parse(obs_sentence)
        labels, _, _ = parser.compute_labels()

        tokens = best_parse.split()
        current_token = tokens[-1]
        current_duration = 0
        while current_duration < len(labels) and labels[-1-current_duration] == int(current_token):
            current_duration += 1
        mu, sigma = duration_prior[current_token]
        new_duration = min(pred_len, max(0, int(mu)-current_duration))
        pred_len -= new_duration
        current_duration = new_duration
        predictions[:current_duration, int(current_token)] = 1
        sequence = obs_sentence
        while pred_len> 0:
            if new_duration != 0:
                prob = np.ones((new_duration, k))*args.grammar_epsilon
                prob[:, int(current_token)] = 1.0
                prob /= sum(prob[0, :])
                sequence = np.vstack((sequence, prob))
            _, _ = parser.parse(sequence)
            predict_prob = parser.future_predict(args.grammar_epsilon)
            current_token = np.argmax(predict_prob, axis=-1)
            mu, sigma = duration_prior[str(current_token)]
            new_duration = min(pred_len, int(mu))
            predictions[current_duration : current_duration + new_duration, int(current_token)] = 1
            current_duration += new_duration
            pred_len -= new_duration

        batch_predictions.append(predictions)
        pred_len = args.pred_len

    return torch.from_numpy(np.stack(batch_predictions))


def obs_parse(batch_data_seq, seq_len, s_gae, As_seq, cluster_obj, nc, device=None):
    if device is not None:
        batch_data_seq = batch_data_seq.to(device)
        s_gae = s_gae.to(device)
        As_seq = As_seq.to(device)

    data = data_feeder_gae(batch_data_seq)
    s_gae.eval()

    feature_seq = []
    for i in range(seq_len):
        A = torch.sum(As_seq[i], 1)
        _, mu, _ = s_gae(data[i], A)
        mu = mu.permute(0, 2, 1).contiguous()
        mu = mu.mean(-1)
        feature_seq.append(mu.data.cpu().numpy())
    feature_seq = np.stack(feature_seq)

    sentence_prob = []
    for i in range(seq_len):
        batch_prob = cluster_prob(feature_seq[i], cluster_obj, nc)
        sentence_prob.append(batch_prob)
    sentence_prob = np.stack(sentence_prob)

    # return torch.from_numpy(sentence_prob)
    return sentence_prob


def make_mlp(dim_list, activation='relu', batch_norm=True, dropout=0):
    layers = []
    for dim_in, dim_out in zip(dim_list[:-1], dim_list[1:]):
        layers.append(nn.Linear(dim_in, dim_out))
        if batch_norm:
            layers.append(nn.BatchNorm1d(dim_out))
        if activation == 'relu':
            layers.append(nn.ReLU())
        elif activation == 'leakyrelu':
            layers.append(nn.LeakyReLU())
        if dropout > 0:
            layers.append(nn.Dropout(p=dropout))
    
    return nn.Sequential(*layers)


def output_activation(x):
    muX = x[:, :, 0:1]
    muY = x[:, :, 1:2]
    sigX = x[:, :, 2:3]
    sigY = x[:, :, 3:4]
    rho = x[:, :, 4:5]
    sigX = torch.exp(sigX)
    sigY = torch.exp(sigY)
    rho = torch.tanh(rho)

    return torch.cat((muX, muY, sigX, sigY, rho), dim=2)


def nll_loss(pred_out, pred_data):
    pred_len, batch = pred_data.size(0), pred_data.size(1)
    acc = torch.zeros_like(pred_data)
    muX = pred_out[:, :, 0]
    muY = pred_out[:, :, 1]
    sigX = pred_out[:, :, 2]
    sigY = pred_out[:, :, 3]
    rho = pred_out[:, :, 4]
    ohr = torch.pow(1-torch.pow(rho, 2), -0.5)

    x = pred_data[:, :, 0]
    y = pred_data[:, :, 1]
    out = torch.pow(ohr, 2)*(torch.pow(sigX, 2)*torch.pow(x-muX, 2) + torch.pow(sigY, 2)*torch.pow(y-muY, 2) - \
          2*rho*torch.pow(sigX, 1)*torch.pow(sigY, 1)*(x-muX)*(y-muY)) - torch.log(sigX*sigY*ohr)
    acc[:, :, 0] = out
    loss = torch.sum(acc)/(pred_len*batch)
    return loss


def mse_loss(pred_out, pred_data):
    pred_len, batch = pred_data.size(0), pred_data.size(1)
    acc = torch.zeros_like(pred_data)
    muX = pred_out[:, :, 0]
    muY = pred_out[:, :, 1]

    x = pred_data[:, :, 0]
    y = pred_data[:, :, 1]
    out = torch.pow(x-muX, 2)+torch.pow(y-muY, 2)
    acc[:, :, 0] = out
    loss = torch.sum(acc)/(pred_len*batch)
    return loss


def displacement_error(pred_traj, pred_traj_gt, mode='avg'):
    loss = pred_traj_gt.permute(1, 0, 2)-pred_traj.permute(1, 0, 2)
    loss = loss**2
    # loss = torch.sqrt(loss.sum(dim=2)).sum(dim=1)
    loss = torch.sqrt(loss.sum(dim=2)).mean(dim=1)

    if mode == 'sum':
        return torch.sum(loss)
    elif mode == 'avg':
        return torch.mean(loss)
    elif mode == 'raw':
        return loss


def final_displacement_error(pred_pos, pred_pos_gt, mode='avg'):
    loss = pred_pos_gt-pred_pos
    loss = loss**2
    loss = torch.sqrt(loss.sum(dim=1))

    if mode == 'sum':
        return torch.sum(loss)
    elif mode == 'avg':
        return torch.mean(loss)
    elif mode == 'raw':
        return loss