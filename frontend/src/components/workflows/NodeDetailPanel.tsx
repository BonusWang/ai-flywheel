import React from 'react'
import { StatusBadge } from '../StatusBadge'
import { STAGE_PLAIN_NAMES } from '../../lib/labels'
import type { NodeExecution } from '../../types'

export const NodeDetailPanel: React.FC<{
  nodes: NodeExecution[]
  activeNode: NodeExecution | null
  onSelectNode: (nodeId: string) => void
}> = ({ nodes, activeNode, onSelectNode }) => (
  <div className="grid grid-cols-1 lg:grid-cols-12 gap-4">
    {/* Compact 8-stage picker */}
    <div className="lg:col-span-5 grid grid-cols-2 gap-2">
      {nodes.map((node) => {
        const isSel = activeNode?.node_id === node.node_id
        const code = node.node_id.split('_')[0]
        return (
          <button
            key={node.node_id}
            type="button"
            onClick={() => onSelectNode(node.node_id)}
            className={`pressable text-left px-3 py-2 rounded-[var(--radius-control)] border ${
              isSel
                ? 'bg-surface border-accent ring-1 ring-accent/35 shadow-[var(--shadow-card)]'
                : 'bg-surface/70 border-line-soft hover:border-line-strong'
            }`}
          >
            <div className="flex items-center justify-between gap-1">
              <span className="mono text-[11px] text-ink-4">{code}</span>
              <StatusBadge status={node.status} />
            </div>
            <div className="text-[12.5px] font-medium text-ink mt-1 truncate">
              {STAGE_PLAIN_NAMES[node.node_id] || node.name}
            </div>
          </button>
        )
      })}
    </div>

    {/* Selected stage detail */}
    <div className="lg:col-span-7 card p-4 space-y-2.5 text-[12.5px]">
      {activeNode ? (
        <div key={activeNode.node_id} className="animate-tab-enter space-y-2.5">
          <div className="flex items-center justify-between gap-2 pb-2 border-b border-line-soft">
            <span className="font-semibold text-ink">
              {activeNode.node_id.split('_')[0]} ·{' '}
              {STAGE_PLAIN_NAMES[activeNode.node_id] || activeNode.name}
            </span>
            <StatusBadge status={activeNode.status} />
          </div>
          <p className="text-ink-2 leading-relaxed">
            {activeNode.summary || '这一步尚未执行。'}
          </p>
          {activeNode.error && (
            <div className="p-2.5 rounded-[var(--radius-control)] bg-hot-soft text-hot border border-hot/25">
              出错原因：{activeNode.error}
            </div>
          )}
          {activeNode.artifacts.length > 0 && (
            <div>
              <div className="text-[11.5px] text-ink-4 mb-1">产出文件</div>
              <div className="flex flex-wrap gap-1.5">
                {activeNode.artifacts.map((art) => (
                  <span
                    key={art}
                    className="mono text-[11px] px-2 py-0.5 rounded-[var(--radius-mark)] bg-ok-soft text-ok-ink border border-ok/25"
                  >
                    {art}
                  </span>
                ))}
              </div>
            </div>
          )}
          {activeNode.internal_checks.length > 0 && (
            <div className="pt-1">
              <div className="text-[11.5px] text-ink-4 mb-1">自动检查项</div>
              <div className="flex flex-wrap gap-1.5">
                {activeNode.internal_checks.map((chk, cIdx) => (
                  <span key={cIdx} className="chip">
                    {chk.name} · {chk.status === 'PASS' ? '通过' : chk.status}
                  </span>
                ))}
              </div>
            </div>
          )}
          {activeNode.payload?.call_telemetry && (
            <div className="mt-1 p-2.5 rounded-[var(--radius-control)] bg-bg-sunk/70 border border-line-soft space-y-1">
              <div className="text-[11.5px] font-semibold text-ink">模型耗时与用量</div>
              {(() => {
                const t = activeNode.payload.call_telemetry
                if (!t) return null
                return (
                  <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11.5px] text-ink-3 num">
                    {t.model && (
                      <span>
                        模型：<code className="mono text-ink-2">{t.model}</code>
                      </span>
                    )}
                    {t.duration_ms != null && (
                      <span>耗时：{(Number(t.duration_ms) / 1000).toFixed(1)}s</span>
                    )}
                    {t.attempts != null && <span>尝试：{t.attempts} 次</span>}
                    {(t.prompt_tokens != null || t.completion_tokens != null) && (
                      <span>
                        Token：输入 {t.prompt_tokens ?? 0} / 输出 {t.completion_tokens ?? 0}
                        {t.reasoning_tokens ? `（思考 ${t.reasoning_tokens}）` : ''}
                      </span>
                    )}
                    {t.cache_hit === true && (
                      <span className="text-ok-ink font-medium">命中缓存</span>
                    )}
                  </div>
                )
              })()}
            </div>
          )}
          {activeNode.node_id === 'P05_SETTLE' && activeNode.payload?.dream && (
            <div className="mt-2 p-3 rounded-[var(--radius-control)] bg-bg-sunk/70 border border-line-soft space-y-1.5">
              <div className="text-[12px] font-semibold text-ink">
                周五精简体检摘要（{activeNode.payload.dream.period_key}）
              </div>
              <div className="flex flex-wrap gap-2 text-[11.5px] text-ink-3 num">
                <span>
                  休眠零命中规则：
                  {(activeNode.payload.dream.auto_dormant_rules || []).length} 条
                </span>
                <span>·</span>
                <span>
                  待复核笔记：
                  {(activeNode.payload.dream.expired_cards || []).length} 篇
                </span>
                <span>·</span>
                <span>
                  待处理分歧：{activeNode.payload.dream.open_dissents_count ?? 0} 条
                </span>
              </div>
              {(activeNode.payload.dream.proposals || []).length > 0 && (
                <ul className="list-disc pl-4 space-y-0.5 text-[12px] text-ink-2">
                  {(activeNode.payload.dream.proposals || []).map((p, pIdx) => (
                    <li key={pIdx}>{p}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      ) : null}
    </div>
  </div>
)
