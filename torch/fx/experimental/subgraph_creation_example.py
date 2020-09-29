import torch
from torch.fx.graph import Graph
from torch.fx.node import Node
from torch.fx.graph_module import GraphModule
from torch.fx.symbolic_trace import symbolic_trace
from typing import Callable, List, Dict, Set, Any, Optional, Tuple

class MyModule(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.param = torch.nn.Parameter(torch.rand(3, 4))
        self.linear = torch.nn.Linear(4, 5)

    def forward(self, x, y):
        z = self.linear(x + self.param).clamp(min=0.0, max=1.0) 
        w = self.linear(y).clamp(min=0.0, max=1.0) 
        return z + w

# Symbolically trace model
my_module = MyModule()
my_module_traced = symbolic_trace(my_module)

class Partition:
    def __init__(self, name: str):
        self.name: str = name
        self.node_names: List[str] = []
        self.inputs: Set[str] = set()
        self.outputs: Set[str] = set()
        self.partitions_dependent_on: Set[str] = set()
        self.partition_dependents: Set[str] = set()
        self.graph : Graph = Graph()
        self.environment : Dict[Node, Node] = {}
        self.targets : Dict[str, Any] = {}

    def __repr__(self) -> str:
        return f"name: {self.name},\n" \
            f" nodes: {self.node_names},\n" \
            f" inputs: {self.inputs},\n" \
            f" outputs: {self.outputs},\n" \
            f" partitions depenent on: {self.partitions_dependent_on},\n" \
            f" parition dependents: {self.partition_dependents}"


# Creates subgraphs out of main graph 
def split_module(
    m: GraphModule,
    root_m: torch.nn.Module,
    split_callback: Callable[[Node], int],
):
    partitions: Dict[str, Partition] = {}
    orig_nodes: Dict[str, Node] = {}

    def record_cross_partition_use(def_node : Node, use_node : Optional[Node]):
        def_partition_name = getattr(def_node, '_fx_partition', None)
        use_partition_name = getattr(use_node, '_fx_partition', None)
        if def_partition_name != use_partition_name:
            if def_partition_name is not None:
                def_partition = partitions[def_partition_name]
                def_partition.outputs.add(def_node.name)
                if use_partition_name is not None:
                    def_partition.partition_dependents.add(use_partition_name)

            if use_partition_name is not None:
                use_partition = partitions[use_partition_name]
                use_partition.inputs.add(def_node.name)
                if def_partition_name is not None:
                    use_partition.partitions_dependent_on.add(def_partition_name)

    # split nodes into parititons
    for node in m.graph.nodes:
        orig_nodes[node.name] = node

        # TODO currently placeholders/parameters aren't put into random partitions,
        # rather they're added to the graphs where they are used down below
        if node.op in ["placeholder", "get_attr"]:
            continue
        partition_name = str(split_callback(node))

        # add node to partitions
        partition = partitions.get(partition_name)
        if partition is None:
            partitions[partition_name] = partition = Partition(partition_name)

        partition.node_names.append(node.name)
        node._fx_partition = partition_name

        torch.fx.graph.map_arg(node.args, lambda def_node: record_cross_partition_use(def_node, node))
        torch.fx.graph.map_arg(node.kwargs, lambda def_node: record_cross_partition_use(def_node, node))

    torch.fx.graph.map_arg(m.graph.result, lambda n: record_cross_partition_use(n, None))

    # find partitions with no dependencies
    root_partitions : List[str] = []
    for partition_name, partition in partitions.items():
        if not len(partition.partitions_dependent_on):
            root_partitions.append(partition_name)

    # check partitions for circular dependencies and create topological partition ordering
    sorted_partitions : List[str] = []
    while root_partitions:
        root_partition = root_partitions.pop()
        sorted_partitions.append(root_partition)
        for dependent in partitions[root_partition].partition_dependents:
            partitions[dependent].partitions_dependent_on.remove(root_partition)
            if not partitions[dependent].partitions_dependent_on:
                root_partitions.append(dependent)
    if len(sorted_partitions) != len(partitions):
        raise RuntimeError("cycle exists between partitions!")

    # add placeholders to parititons
    for partition_name in sorted_partitions:
        partition = partitions[partition_name]
        for input in partition.inputs:
            placeholder = partition.graph.placeholder(input)
            partition.environment[orig_nodes[input]] = placeholder

    # Transform nodes and collect targets for partition's submodule
    for node in m.graph.nodes:
        if hasattr(node, '_fx_partition'):
            partition = partitions[node._fx_partition]

            # swap out old graph nodes in kw/args with references to new nodes in this submodule
            environment = partition.environment
            gathered_args = torch.fx.graph.map_arg(node.args, lambda n : environment[n])
            gathered_kwargs = torch.fx.graph.map_arg(node.kwargs, lambda n : environment[n])

            if node.op not in ['call_module', 'get_attr']:
                target = node.target
            else:
                target_atoms = node.target.split('.')
                target_attr = m
                for atom in target_atoms:
                    if not hasattr(target_attr, atom):
                        raise RuntimeError(f'Operator target {node.target} not found!')
                    target_attr = getattr(target_attr, atom)
                partition.targets[node.target] = target_attr
                target = target_atoms[-1]

            new_node = partition.graph.create_node(op=node.op, target=target, args=gathered_args,  # type: ignore 
                                                   kwargs=gathered_kwargs)  # type: ignore  
            partition.environment[node] = new_node

    # Set up values to construct base module
    base_mod_env : Dict[str, Node] = {}
    base_mod_graph : Graph = Graph()
    base_mod_attrs : Dict[str, GraphModule] = {}
    for node in m.graph.nodes:
        if node.op == 'placeholder':
            base_mod_env[node.name] = base_mod_graph.placeholder(node.name)
        elif node.op == 'get_attr':
            base_mod_env[node.name] = base_mod_graph.get_attr(node.target)
            attr_val = m
            for atom in node.target.split('.'):
                if not hasattr(attr_val, atom):
                    raise RuntimeError(f'Node target {node.target} not found!')
                attr_val = getattr(attr_val, atom)
            base_mod_attrs[node.target] = attr_val

    # Do some things iterating over the partitions in topological order again:
    # 1) Finish off submodule Graphs by setting corresponding outputs
    # 2) Construct GraphModules for each submodule
    # 3) Construct the base graph by emitting calls to those submodules in
    #    topological order

    for partition_name in sorted_partitions:
        partition = partitions[partition_name]

        # Set correct output values
        output_vals = tuple(partition.environment[orig_nodes[name]] for name in partition.outputs)
        partition.graph.output(output_vals)

        # Construct GraphModule for this partition
        submod_name = f'submod_{partition_name}'
        base_mod_attrs[submod_name] = GraphModule(partition.targets, partition.graph)

        # Emit call in base graph to this submodule
        output_args : Optional[Tuple[torch.fx.node.Argument, ...]] = tuple([base_mod_env[name] for name in partition.inputs])
        output_val = base_mod_graph.call_module(submod_name, output_args)
        if len(partition.outputs) > 1:
            # Unpack multiple return values from submodule
            output_val_proxy = torch.fx.proxy.Proxy(output_val)
            for i, output_name in enumerate(partition.outputs):
                base_mod_env[output_name] = output_val_proxy[i].node  # type: ignore
        else:
            base_mod_env[list(partition.outputs)[0]] = output_val

    # Set output value for base graph
    base_mod_graph.output(torch.fx.graph.map_arg(m.graph.result, lambda n : base_mod_env[n.name]))

    return GraphModule(base_mod_attrs, base_mod_graph)

# random mod partitioning
partition_counter = 0
NPARTITIONS = 3
def mod_partition(node: Node):
    global partition_counter
    partition = partition_counter % NPARTITIONS
    partition_counter = (partition_counter + 1) % NPARTITIONS
    return partition


split_graph = split_module(my_module_traced, my_module, mod_partition)

x = torch.rand(3, 4)
y = torch.rand(3, 4)

orig_out = my_module_traced(x, y)
subgraphs_out = split_graph(x, y)

print(orig_out)
print()
print(subgraphs_out)
