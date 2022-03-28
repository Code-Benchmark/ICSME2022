import torch
import torch.nn as nn
import torch.nn.functional as F
import math
from torch.autograd import Variable
# from torchsummary import summary

def truncated_normal_(tensor, mean=0, std=0.09):  # https://zhuanlan.zhihu.com/p/83609874  tf.trunc_normal()

    size = tensor.shape
    tmp = tensor.new_empty(size + (4,)).normal_()
    valid = (tmp < 2) & (tmp > -2)
    ind = valid.max(-1, keepdim=True)[1]
    tensor.data.copy_(tmp.gather(-1, ind).squeeze(-1))
    tensor.data.mul_(std).add_(mean)
    return tensor

def th_gather_nd( x, indices):
    newshape = indices.shape[:-1] + x.shape[indices.shape[-1]:]
    indices = indices.view(-1, indices.shape[-1]).tolist()
    out = torch.cat([x.__getitem__(tuple(i)) for i in indices])
    return out.reshape(newshape)

def eta_t(children):
    """Compute weight matrix for how much each vector belongs to the 'top'"""
    # children is shape (batch_size x max_tree_size x max_children)
    batch_size = children.size(0)
    max_tree_size = children.size(1)
    max_children = children.size(2)
    # eta_t is shape (batch_size x max_tree_size x max_children + 1)
    return (torch.unsqueeze(torch.cat(
        [torch.ones((max_tree_size, 1)).to(children.device), torch.zeros((max_tree_size, max_children)).to(children.device)],
        dim=1), dim=0,
        )).repeat([batch_size, 1, 1])

def eta_r(children, t_coef):
    """Compute weight matrix for how much each vector belogs to the 'right'"""
    children = children.type(torch.float32)
    batch_size = children.size(0)
    max_tree_size = children.size(1)
    max_children = children.size(2)

    # num_siblings is shape (batch_size x max_tree_size x 1)
    num_siblings = torch.sum((~(children == 0)).int(),dim=2,keepdim=True,dtype=torch.float32)
    # num_siblings is shape (batch_size x max_tree_size x max_children + 1)
    num_siblings = num_siblings.repeat(([1, 1, max_children + 1]))

    # creates a mask of 1's and 0's where 1 means there is a child there
    # has shape (batch_size x max_tree_size x max_children + 1)
    mask = torch.cat(
        [torch.zeros((batch_size, max_tree_size, 1)).to(children.device),
         torch.min(children, torch.ones((batch_size, max_tree_size, max_children)).to(children.device))],
        dim=2)
    # child indices for every tree (batch_size x max_tree_size x max_children + 1)
    child_indices = torch.mul(
        (torch.unsqueeze(
            torch.unsqueeze(
                # torch.arange(-1.0, max_children.type(torch.float32),1.0, dtype=torch.float32),
                torch.arange(-1.0, torch.tensor(max_children, dtype=torch.float32),1.0, dtype=torch.float32),
                dim=0),
        dim=0).repeat([batch_size, max_tree_size, 1])).cuda(),
        mask
    )

    # weights for every tree node in the case that num_siblings = 0
    # shape is (batch_size x max_tree_size x max_children + 1)
    singles = torch.cat(
        [torch.zeros((batch_size, max_tree_size, 1)).to(children.device),
         torch.full((batch_size, max_tree_size, 1), 0.5).to(children.device),
         torch.zeros((batch_size, max_tree_size, max_children - 1)).to(children.device)],
        dim=2)

    # eta_r is shape (batch_size x max_tree_size x max_children + 1)
    return torch.where(
        # torch.equal(num_siblings, 1.0),
        torch.eq(num_siblings, 1.0),
        # avoid division by 0 when num_siblings == 1
        singles,
        # the normal case where num_siblings != 1
        (1.0 - t_coef) * (child_indices / (num_siblings - 1.0))
    )

def eta_l(children, coef_t, coef_r):
    """Compute weight matrix for how much each vector belongs to the 'left'"""
    children = children.type(torch.float32)
    batch_size = children.size(0)
    max_tree_size = children.size(1)
    max_children = children.size(2)

    # creates a mask of 1's and 0's where 1 means there is a child there
    # has shape (batch_size x max_tree_size x max_children + 1)
    mask = torch.cat(
        [torch.zeros((batch_size, max_tree_size, 1)).to(children.device),
         torch.min(children, torch.ones((batch_size, max_tree_size, max_children)).to(children.device))],
        dim=2)

    # eta_l is shape (batch_size x max_tree_size x max_children + 1)

    return torch.mul(
        torch.mul((1.0 - coef_t), (1.0 - coef_r)),mask
    )

def children_tensor( nodes, children, feature_size):
    max_children = torch.tensor(children.size(2)).cuda()
    batch_size = torch.tensor(nodes.size(0)).cuda()
    num_nodes = torch.tensor(nodes.size(1)).cuda()

    # replace the root node with the zero vector so lookups for the 0th
    # vector return 0 instead of the root vector
    # zero_vecs is (batch_size, num_nodes, 1)
    zero_vecs = torch.zeros((batch_size, 1, feature_size)).cuda()
    # vector_lookup is (batch_size x num_nodes x feature_size)
    vector_lookup = torch.cat([zero_vecs, nodes[:, 1:, :]], dim=1)
    # children is (batch_size x num_nodes x num_children x 1)
    children = torch.unsqueeze(children, dim=3)
    # prepend the batch indices to the 4th dimension of children
    # batch_indices is (batch_size x 1 x 1 x 1)
    batch_indices = torch.reshape(torch.arange(0, batch_size), (batch_size, 1, 1, 1)).cuda()
    batch_indices = batch_indices.repeat([1, num_nodes, max_children, 1])
    # batch_indices is (batch_size x num_nodes x num_children x 1)        batch_indices = batch_size.repeat(1, num_nodes, max_children, 1)
    # children is (batch_size x num_nodes x num_children x 2)
    children = torch.cat([batch_indices, children], dim=3)
    # output will have shape (batch_size x num_nodes x num_children x feature_size)
    return th_gather_nd(vector_lookup, children)

def conv_step(nodes, children,feature_size,w_t, w_l, w_r, b_conv):
    # nodes is shape (batch_size x max_tree_size x feature_size)
    # children is shape (batch_size x max_tree_size x max_children)

    # children_vectors will have shape
    # (batch_size x max_tree_size x max_children x feature_size)
    children_vectors = children_tensor(nodes, children, feature_size)

    # add a 4th dimension to the nodes tensor
    nodes = torch.unsqueeze(nodes, 2)

    # tree_tensor is shape
    # (batch_size x max_tree_size x max_children + 1 x feature_size)
    tree_tensor = torch.cat([nodes, children_vectors], dim=2)

    # coefficient tensors are shape (batch_size x max_tree_size x max_children + 1)
    c_t = eta_t(children)

    c_r = eta_r(children, c_t)
    c_l = eta_l(children, c_t, c_r)

    # concatenate the position coefficients into a tensor
    # (batch_size x max_tree_size x max_children + 1 x 3)
    coef = torch.stack([c_t, c_r, c_l], dim=3)
    weights = torch.stack([w_t, w_r, w_l], dim=0)

    batch_size = children.size(0)
    max_tree_size = children.size(1)
    max_children = children.size(2)

    # reshape for matrix multiplication
    x = batch_size * max_tree_size
    y = max_children + 1

    result = tree_tensor.reshape(x, y, feature_size)
    coef = coef.reshape(x, y, 3)
    result = torch.matmul(result.transpose(1, 2), coef)
    result = torch.reshape(result, (batch_size, max_tree_size, 3, feature_size))

    result = torch.tensordot(result, weights, [[2, 3], [0, 1]])

    return torch.tanh(result + b_conv)


def pool_layer(nodes):
    """Creates a max dynamic pooling layer from the nodes."""
    pooled = torch.max(nodes, 1)
    return pooled.values


class TBCNN(nn.Module):
    def __init__(self, feature_size, label_size,conv_feature,w_t, w_l, w_r, b_conv, w_h, b_h):
        super(TBCNN, self).__init__()

        self.feature_size = feature_size
        self.label_size = label_size
        self.conv_feature = conv_feature

        self.w_t = torch.nn.Parameter(w_t)
        self.w_l = torch.nn.Parameter(w_l)
        self.w_r = torch.nn.Parameter(w_r)
        self.b_conv = torch.nn.Parameter(b_conv)
        self.w_h = torch.nn.Parameter(w_h)
        self.b_h = torch.nn.Parameter(b_h)

    def hidden_layer(self,pooled):

        return torch.tanh(torch.matmul(pooled, self.w_h) + self.b_h)

    def forward(self, nodes, children):
        nodes = torch.tensor(nodes)
        children = torch.tensor(children)
        conv = [
            conv_step(nodes, children, self.feature_size, self.w_t, self.w_l,self.w_r, self.b_conv)
            for _ in range(1)
        ]
        conv = torch.cat(conv, dim=2)
        pooling = pool_layer(conv)
        hidden = self.hidden_layer(pooling)
        # hidden = torch.tanh(torch.matmul(pooling, weights) + bias)
        out = torch.softmax(hidden, dim=-1)

        return out







