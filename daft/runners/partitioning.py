from __future__ import annotations

import dataclasses
from abc import abstractmethod
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable, Dict, Generic, List, Optional, Tuple, TypeVar

import numpy as np
import pandas as pd
import pyarrow as pa

from daft.expressions import ColID, Expression, ExpressionExecutor
from daft.logical.schema import ExpressionList
from daft.runners.blocks import ArrowArrType, DataBlock, PyListDataBlock

from ..execution.operators import OperatorEnum, PythonExpressionType

PartID = int


@dataclass(frozen=True)
class PyListTile:
    column_id: ColID
    column_name: str
    partition_id: PartID
    block: DataBlock

    def __len__(self) -> int:
        return len(self.block)

    def apply(self, func: Callable[[DataBlock], DataBlock]) -> PyListTile:
        return dataclasses.replace(self, block=func(self.block))

    def split_by_index(self, num_partitions: int, target_partition_indices: DataBlock) -> List[PyListTile]:
        assert len(target_partition_indices) == len(self)
        new_blocks = self.block.partition(num_partitions, targets=target_partition_indices)
        assert len(new_blocks) == num_partitions
        return [dataclasses.replace(self, block=nb, partition_id=i) for i, nb in enumerate(new_blocks)]

    @classmethod
    def merge_tiles(cls, to_merge: List[PyListTile], verify_partition_id: bool = True) -> PyListTile:
        assert len(to_merge) > 0

        if len(to_merge) == 1:
            return to_merge[0]

        column_id = to_merge[0].column_id
        partition_id = to_merge[0].partition_id
        column_name = to_merge[0].column_name
        # first perform sanity check
        for part in to_merge[1:]:
            assert part.column_id == column_id
            assert not verify_partition_id or part.partition_id == partition_id
            assert part.column_name == column_name

        merged_block = DataBlock.merge_blocks([t.block for t in to_merge])
        return dataclasses.replace(to_merge[0], block=merged_block)

    def replace_block(self, block: DataBlock) -> PyListTile:
        return dataclasses.replace(self, block=block)


@dataclass(frozen=True)
class vPartition:
    columns: Dict[ColID, PyListTile]
    partition_id: PartID

    def __post_init__(self) -> None:
        size = None
        for col_id, tile in self.columns.items():
            if tile.partition_id != self.partition_id:
                raise ValueError(f"mismatch of partition id: {tile.partition_id} vs {self.partition_id}")
            if size is None:
                size = len(tile)
            if len(tile) != size:
                raise ValueError(f"mismatch of tile lengths: {len(tile)} vs {size}")
            if col_id != tile.column_id:
                raise ValueError(f"mismatch of column id: {col_id} vs {tile.column_id}")

    def __len__(self) -> int:
        assert len(self.columns) > 0
        return len(next(iter(self.columns.values())))

    def eval_expression(self, expr: Expression) -> PyListTile:
        expr_col_id = expr.get_id()
        expr_name = expr.name()

        assert expr_col_id is not None
        assert expr_name is not None
        if not expr.has_call():
            return PyListTile(
                column_id=expr_col_id,
                column_name=expr_name,
                partition_id=self.partition_id,
                block=self.columns[expr_col_id].block,
            )

        required_cols = expr.required_columns()
        required_blocks = {}
        for c in required_cols:
            col_id = c.get_id()
            assert col_id is not None
            block = self.columns[col_id].block
            name = c.name()
            assert name is not None
            required_blocks[name] = block
        exec = ExpressionExecutor()
        result = exec.eval(expr, required_blocks)
        expr_col_id = expr.get_id()
        expr_name = expr.name()
        assert expr_col_id is not None
        assert expr_name is not None
        return PyListTile(column_id=expr_col_id, column_name=expr_name, partition_id=self.partition_id, block=result)

    def eval_expression_list(self, exprs: ExpressionList) -> vPartition:
        tile_list = [self.eval_expression(e) for e in exprs]
        new_columns = {t.column_id: t for t in tile_list}
        return vPartition(columns=new_columns, partition_id=self.partition_id)

    @classmethod
    def from_arrow_table(cls, table: pa.Table, column_ids: List[ColID], partition_id: PartID) -> vPartition:
        names = table.column_names
        assert len(names) == len(column_ids)
        tiles = {}
        for i, (col_id, name) in enumerate(zip(column_ids, names)):
            arr = table[i]
            block: DataBlock[ArrowArrType] = DataBlock.make_block(arr)
            tiles[col_id] = PyListTile(column_id=col_id, column_name=name, partition_id=partition_id, block=block)
        return vPartition(columns=tiles, partition_id=partition_id)

    @classmethod
    def from_pydict(cls, data: Dict[str, List[Any]], schema: ExpressionList, partition_id: PartID) -> vPartition:
        column_exprs = schema.to_column_expressions()
        tiles = {}
        for col_expr in column_exprs:
            col_id = col_expr.get_id()
            col_name = col_expr.name()
            arr = (
                data[col_name]
                if isinstance(col_expr.resolved_type(), PythonExpressionType)
                else pa.array(data[col_expr.name()])
            )
            block: DataBlock = DataBlock.make_block(arr)
            tiles[col_id] = PyListTile(column_id=col_id, column_name=col_name, partition_id=partition_id, block=block)
        return vPartition(columns=tiles, partition_id=partition_id)

    def to_pandas(self, schema: Optional[ExpressionList] = None) -> pd.DataFrame:
        if schema is not None:
            output_schema = [(expr.name(), expr.get_id()) for expr in schema]
        else:
            output_schema = [(tile.column_name, id) for id, tile in self.columns.items()]
        return pd.DataFrame(
            {
                name: pd.Series(self.columns[id].block.data)
                if isinstance(self.columns[id].block, PyListDataBlock)
                else self.columns[id].block.data.to_pandas()
                for name, id in output_schema
            }
        )

    def for_each_column_block(self, func: Callable[[DataBlock], DataBlock]) -> vPartition:
        return dataclasses.replace(self, columns={col_id: col.apply(func) for col_id, col in self.columns.items()})

    def head(self, num: int) -> vPartition:
        # TODO make optimization for when num=0
        return self.for_each_column_block(partial(DataBlock.head, num=num))

    def sample(self, num: int) -> vPartition:
        if len(self) == 0:
            return self
        sample_idx: DataBlock[ArrowArrType] = DataBlock.make_block(data=np.random.randint(0, len(self), num))
        return self.for_each_column_block(partial(DataBlock.take, indices=sample_idx))

    def filter(self, predicate: ExpressionList) -> vPartition:
        mask_list = self.eval_expression_list(predicate)
        assert len(mask_list.columns) > 0
        mask = next(iter(mask_list.columns.values())).block
        for to_and in mask_list.columns.values():
            mask = mask.run_binary_operator(to_and.block, OperatorEnum.AND)
        return self.for_each_column_block(partial(DataBlock.filter, mask=mask))

    def sort(self, sort_key: Expression, desc: bool = False) -> vPartition:
        sort_tile = self.eval_expression(sort_key)
        argsort_idx = sort_tile.apply(partial(DataBlock.argsort, desc=desc))
        return self.take(argsort_idx.block)

    def take(self, indices: DataBlock) -> vPartition:
        return self.for_each_column_block(partial(DataBlock.take, indices=indices))

    def agg(self, to_agg: List[Tuple[Expression, str]], group_by: Optional[ExpressionList] = None) -> vPartition:
        evaled_expressions = self.eval_expression_list(ExpressionList([e for e, _ in to_agg]))
        ops = [op for _, op in to_agg]
        if group_by is None:
            agged = {}
            for op, (col_id, tile) in zip(ops, evaled_expressions.columns.items()):
                agged[col_id] = tile.apply(func=partial(tile.block.__class__.agg, op=op))
            return vPartition(partition_id=self.partition_id, columns=agged)
        else:
            grouped_blocked = self.eval_expression_list(group_by)
            assert len(evaled_expressions.columns) == len(ops)
            gcols, acols = DataBlock.group_by_agg(
                list(tile.block for tile in grouped_blocked.columns.values()),
                list(tile.block for tile in evaled_expressions.columns.values()),
                agg_ops=ops,
            )
            new_columns = {}

            for block, (col_id, tile) in zip(gcols, grouped_blocked.columns.items()):
                new_columns[col_id] = dataclasses.replace(tile, block=block)

            for block, (col_id, tile) in zip(acols, evaled_expressions.columns.items()):
                new_columns[col_id] = dataclasses.replace(tile, block=block)
            return vPartition(partition_id=self.partition_id, columns=new_columns)

    def split_by_hash(self, exprs: ExpressionList, num_partitions: int) -> List[vPartition]:
        values_to_hash = self.eval_expression_list(exprs)
        keys = list(values_to_hash.columns.keys())
        keys.sort()
        hsf = None
        assert len(keys) > 0
        for k in keys:
            block = values_to_hash.columns[k].block
            hsf = block.array_hash(seed=hsf)
        assert hsf is not None
        target_idx = hsf.run_binary_operator(num_partitions, OperatorEnum.MOD)
        return self.split_by_index(num_partitions, target_partition_indices=target_idx)

    def split_by_index(self, num_partitions: int, target_partition_indices: DataBlock) -> List[vPartition]:
        assert len(target_partition_indices) == len(self)
        new_partition_to_columns: List[Dict[ColID, PyListTile]] = [{} for _ in range(num_partitions)]
        for col_id, tile in self.columns.items():
            new_tiles = tile.split_by_index(
                num_partitions=num_partitions, target_partition_indices=target_partition_indices
            )
            for part_id, nt in enumerate(new_tiles):
                new_partition_to_columns[part_id][col_id] = nt

        return [vPartition(partition_id=i, columns=columns) for i, columns in enumerate(new_partition_to_columns)]

    def join(
        self,
        right: vPartition,
        left_on: ExpressionList,
        right_on: ExpressionList,
        output_schema: ExpressionList,
        how: str = "inner",
    ) -> vPartition:
        assert how == "inner"
        left_key_part = self.eval_expression_list(left_on)
        left_key_ids = list(left_key_part.columns.keys())

        right_key_part = right.eval_expression_list(right_on)
        left_key_list = [left_key_part.columns[le.get_id()].block for le in left_on]
        right_key_list = [right_key_part.columns[re.get_id()].block for re in right_on]

        left_nonjoin_ids = [i for i in self.columns.keys() if i not in left_key_part.columns]
        right_nonjoin_ids = [i for i in right.columns.keys() if i not in right_key_part.columns]

        left_nonjoin_blocks = [self.columns[i].block for i in left_nonjoin_ids]
        right_nonjoin_blocks = [right.columns[i].block for i in right_nonjoin_ids]

        joined_blocks = DataBlock.join(left_key_list, right_key_list, left_nonjoin_blocks, right_nonjoin_blocks)

        result_keys = left_key_ids + left_nonjoin_ids + right_nonjoin_ids

        assert len(joined_blocks) == len(result_keys)
        joined_block_idx = 0
        result_columns = {}
        for k in left_key_ids:
            result_columns[k] = left_key_part.columns[k].replace_block(block=joined_blocks[joined_block_idx])
            joined_block_idx += 1

        for k in left_nonjoin_ids:
            result_columns[k] = self.columns[k].replace_block(block=joined_blocks[joined_block_idx])
            joined_block_idx += 1

        for k in right_nonjoin_ids:
            result_columns[k] = right.columns[k].replace_block(block=joined_blocks[joined_block_idx])
            joined_block_idx += 1

        assert joined_block_idx == len(result_keys)

        output = vPartition(columns=result_columns, partition_id=self.partition_id)
        return output.eval_expression_list(output_schema)

    @classmethod
    def merge_partitions(cls, to_merge: List[vPartition], verify_partition_id: bool = True) -> vPartition:
        assert len(to_merge) > 0

        if len(to_merge) == 1:
            return to_merge[0]

        pid = to_merge[0].partition_id
        col_ids = set(to_merge[0].columns.keys())
        # first perform sanity check
        for part in to_merge[1:]:
            assert not verify_partition_id or part.partition_id == pid
            assert set(part.columns.keys()) == col_ids

        new_columns = {}
        for col_id in to_merge[0].columns.keys():
            new_columns[col_id] = PyListTile.merge_tiles(
                [vp.columns[col_id] for vp in to_merge], verify_partition_id=verify_partition_id
            )
        return dataclasses.replace(to_merge[0], columns=new_columns)


PartitionT = TypeVar("PartitionT")


class PartitionSet(Generic[PartitionT]):
    @abstractmethod
    def to_pandas(self, schema: Optional[ExpressionList] = None) -> pd.DataFrame:
        raise NotImplementedError()

    @abstractmethod
    def get_partition(self, idx: PartID) -> PartitionT:
        raise NotImplementedError()

    @abstractmethod
    def set_partition(self, idx: PartID, part: PartitionT) -> None:
        raise NotImplementedError()

    @abstractmethod
    def delete_partition(self, idx: PartID) -> None:
        raise NotImplementedError()

    @abstractmethod
    def has_partition(self, idx: PartID) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def __len__(self) -> int:
        return sum(self.len_of_partitions())

    @abstractmethod
    def len_of_partitions(self) -> List[int]:
        raise NotImplementedError()

    @abstractmethod
    def num_partitions(self) -> int:
        raise NotImplementedError()


class PartitionManager:
    def __init__(self, pset_default: Callable[[], PartitionSet]) -> None:
        self._nid_to_partition_set: Dict[int, PartitionSet] = {}
        self._pset_default_list = [pset_default]

    def new_partition_set(self) -> PartitionSet:
        func = self._pset_default_list[0]
        return func()

    def get_partition_set(self, node_id: int) -> PartitionSet:
        assert node_id in self._nid_to_partition_set
        return self._nid_to_partition_set[node_id]

    def put_partition_set(self, node_id: int, pset: PartitionSet) -> None:
        self._nid_to_partition_set[node_id] = pset

    def rm(self, node_id: int, partition_id: Optional[int] = None):
        if partition_id is None:
            del self._nid_to_partition_set[node_id]
        else:
            self._nid_to_partition_set[node_id].delete_partition(partition_id)
            if self._nid_to_partition_set[node_id].num_partitions() == 0:
                del self._nid_to_partition_set[node_id]

    def clear(self) -> None:
        del self._nid_to_partition_set
        self._nid_to_partition_set = {}
