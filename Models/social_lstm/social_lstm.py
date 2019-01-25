import torch
import torch.nn as nn
import numpy as np

class SocialLSTM(nn.Module):
    def __init__(self, args):
        super(SocialLSTM, self).__init__()

        self.use_cuda = args.use_cuda
        self.hidden_size = args.hidden_size
        self.grid_size = args.grid_size
        self.embedding_size = args.embedding_size
        self.input_size = args.input_size
        self.output_size = args.output_size
        self.gru = args.gru

        self.cell = nn.LSTMCell(2*self.embedding_size, self.hidden_size)
        if self.gru:
            self.cell = nn.GRUCell(2*self.embedding_size, self.hidden_size)

        self.input_embedding_layer = nn.Linear(self.input_size, self.embedding_size)
        self.tensor_embedding_layer = nn.Linear(self.grid_size*self.grid_size*self.hidden_size, self.embedding_size)
        self.output_layer = nn.Linear(self.hidden_size, self.output_size)

        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(args.dropout)

        if self.use_cuda:
            self.to(torch.device('cuda:0'))


    def getSocialTensor(self, grid, hidden_states):
        num_nodes = grid.size()[0]

        social_tensor = torch.zeros(num_nodes, self.grid_size*self.grid_size, self.hidden_size)
        if self.use_cuda:
            social_tensor = social_tensor.cuda()

        for node in range(num_nodes):
            social_tensor[node] = torch.mm(torch.t(grid[node]), hidden_states)
        
        social_tensor = social_tensor.view(num_nodes, self.grid_size*self.grid_size*self.hidden_size)
        return social_tensor


    def forward(self, *args):
        input_data = args[0]
        grids = args[1]
        hidden_states = args[2]
        cell_states = args[3]
        seq_len = args[4]
        num_nodes = args[5]

        outputs = torch.zeros(seq_len*num_nodes, self.output_size)
        if self.use_cuda:
            outputs = outputs.cuda()

        for framenum, frame in input_data:
            nodes_current = frame
            grid_current = grids[framenum]
            
            social_tensor = self.getSocialTensor(grid_current, hidden_states)
            input_embedding = self.dropout(self.relu(self.input_embedding_layer(nodes_current)))
            social_embedding = self.dropout(self.relu(self.tensor_embedding_layer(social_tensor)))
            concat_embedding = torch.cat((input_embedding, social_embedding), 1)

            if not self.gru:
                hidden_states, cell_states = self.cell(concat_embedding, (hidden_states, cell_states))
            else:
                hidden_states = self.cell(concat_embedding, hidden_states)
            
            outputs[framenum*num_nodes+torch.tensor(range(num_nodes)).long()] = self.output_layer(hidden_states)
        
        outputs = outputs.view(seq_len, num_nodes, self.output_size)
        return outputs, hidden_states, cell_states

