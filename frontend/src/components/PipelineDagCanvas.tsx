import React from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Loader2,
  MinusCircle,
  PauseCircle,
  ShieldCheck,
  XCircle,
} from 'lucide-react'
import type { NodeExecution, WorkflowRunRecord } from '../types'

interface Props {
  nodes: NodeExecution[]
  runStatus?: WorkflowRunRecord['status']
  selectedNodeId: string | null
  onSelectNode: (nodeId: string) => void
}

function NodeIcon({ status }: { status: NodeExecution['status'] }) {
  switch (status) {
    case 'PASS':
      return <CheckCircle2 className="w-4 h-4 text-ok" />
    case 'RUNNING':
      return <Loader2 className="w-4 h-4 text-accent animate-spin" />
    case 'AWAITING_APPROVAL':
      return <PauseCircle className="w-4 h-4 text-amber animate-pulse" />
    case 'FAILED':
      return <XCircle className="w-4 h-4 text-hot" />
    case 'PARTIAL':
      return <AlertTriangle className="w-4 h-4 text-amber" />
    case 'SKIPPED':
      return <MinusCircle className="w-4 h-4 text-ink-4" />
    default:
      return <Clock className="w-4 h-4 text-ink-4" />
  }
}

const TILE_BY_STATUS: Record<string, string> = {
  PASS: 'border-ok/35 bg-ok-soft/45',
  RUNNING: 'border-accent/50 bg-accent-soft/60',
  AWAITING_APPROVAL: 'border-amber/50 bg-amber-soft/70',
  FAILED: 'border-hot/45 bg-hot-soft/60',
  PARTIAL: 'border-amber/40 bg-amber-soft/45',
  SKIPPED: 'border-line-soft bg-bg-sunk/60 opacity-80',
  PENDING: 'border-line bg-surface',
}

function resolveGateState(nodes: NodeExecution[], runStatus?: WorkflowRunRecord['status']) {
  const p04 = nodes.find((n) => n.node_id === 'P04_WRITE_VERIFY')
  if (!p04) {
    return {
      chip: '待初始化',
      pill: '等待进入审批门禁',
      pillClass: 'bg-bg-sunk text-ink-3',
      rightHint: '进入 P04 受控落盘与回读校验',
    }
  }
  if (p04.status === 'AWAITING_APPROVAL' || runStatus === 'AWAITING_APPROVAL') {
    return {
      chip: '当前挂起等待批准',
      pill: '⏸ 等待人工审批放行',
      pillClass: 'bg-amber-soft text-amber-ink',
      rightHint: '放行后执行 P04 受控落盘',
    }
  }
  if (p04.status === 'PASS') {
    return {
      chip: '已放行并落盘',
      pill: '✓ 审批已放行',
      pillClass: 'bg-ok-soft text-ok-ink',
      rightHint: '已完成 P04 受控落盘与回读校验',
    }
  }
  if (p04.status === 'FAILED') {
    return {
      chip: '落盘校验失败',
      pill: '✕ P04 校验失败',
      pillClass: 'bg-hot-soft text-hot',
      rightHint: '已阻断后续节点并自动释锁',
    }
  }
  if (p04.status === 'SKIPPED') {
    return {
      chip: '已拒绝放行',
      pill: '✕ 审核拒绝，跳过落盘',
      pillClass: 'bg-hot-soft text-hot',
      rightHint: 'P04–P07 已跳过并释放锁',
    }
  }
  return {
    chip: '等待前置阶段完成',
    pill: '待进入审批门禁',
    pillClass: 'bg-bg-sunk text-ink-3',
    rightHint: '进入 P04 受控落盘与回读校验',
  }
}

export const PipelineDagCanvas: React.FC<Props> = ({
  nodes,
  runStatus,
  selectedNodeId,
  onSelectNode,
}) => {
  const firstHalf = nodes.slice(0, 4)
  const secondHalf = nodes.slice(4, 8)
  const gate = resolveGateState(nodes, runStatus)

  const renderNode = (node: NodeExecution) => {
    const isSel = selectedNodeId === node.node_id
    const statusCls = TILE_BY_STATUS[node.status] || TILE_BY_STATUS.PENDING
    return (
      <button
        key={node.node_id}
        type="button"
        onClick={() => onSelectNode(node.node_id)}
        className={`text-left p-3 rounded-[var(--radius-tile)] border transition-all ${statusCls} ${
          isSel ? 'ring-2 ring-accent/40 border-accent' : 'hover:border-line-strong'
        }`}
      >
        <div className="flex items-center justify-between gap-2 mb-1">
          <span className="mono text-[11px] font-bold text-ink-3">
            {node.node_id.split('_')[0]}
          </span>
          <NodeIcon status={node.status} />
        </div>
        <div className="text-[13px] font-semibold text-ink leading-snug">{node.name}</div>
        <div className="text-[11.5px] text-ink-3 mt-1 line-clamp-2 min-h-[1.85rem]">
          {node.summary || '等待前置节点放行'}
        </div>
        <div className="mt-1.5 pt-1.5 border-t border-line-soft flex items-center justify-between text-[11px] text-ink-4 mono">
          <span className="truncate max-w-[115px]">{node.actor}</span>
          <span>{node.duration_ms ? `${node.duration_ms}ms` : '--'}</span>
        </div>
      </button>
    )
  }

  return (
    <section className="card p-4">
      <div className="flex flex-wrap items-center justify-between gap-2 pb-3 mb-3 border-b border-line-soft">
        <div>
          <h2 className="text-[14.5px] font-bold text-ink">后台飞轮 8 阶段执行画布（P00–P07）</h2>
          <p className="text-[12px] text-ink-4 mt-0.5">
            点击任意节点查看检查项、写入清单与不可变 SHA-256 回执
          </p>
        </div>
        <span className="chip">
          <ShieldCheck className="w-3.5 h-3.5 text-accent" />
          写入审批门禁：{gate.chip}
        </span>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2.5">
        {firstHalf.map(renderNode)}
      </div>

      <div className="my-2.5 flex items-center justify-center">
        <div className="flex flex-wrap items-center justify-center gap-2 px-3.5 py-1 rounded-full text-[11.5px] border border-dashed border-line-strong bg-bg-sunk/70 text-ink-3">
          <span>P03 写入计划</span>
          <span>→</span>
          <span className={`px-2 py-0.5 rounded-full font-semibold ${gate.pillClass}`}>
            {gate.pill}
          </span>
          <span>→</span>
          <span>{gate.rightHint}</span>
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2.5">
        {secondHalf.map(renderNode)}
      </div>
    </section>
  )
}
