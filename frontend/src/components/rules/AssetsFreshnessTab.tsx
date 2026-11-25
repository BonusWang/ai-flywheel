import React from 'react'
import { formatDateYMD } from '../../lib/labels'
import type { KnowledgeDocSummary, SkillAssetRecord } from '../../types'

/**
 * The "Skill 与保鲜" tab: the Skill effectiveness ledger (anti Skill theatre) and
 * the knowledge card freshness table (30 / 90 / 180 day recheck windows).
 */
export const AssetsFreshnessTab: React.FC<{
  skills: SkillAssetRecord[]
  knowledgeDocs: KnowledgeDocSummary[]
}> = ({ skills, knowledgeDocs }) => (
  <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
    <section className="card p-5 space-y-3">
      <div className="flex items-center justify-between gap-2 pb-2.5 border-b border-line-soft">
        <div>
          <h2 className="text-[15px] font-bold text-ink">Skill 实效账本（反 Skill 表演）</h2>
          <p className="text-[12px] text-ink-4 mt-0.5">
            追踪每个本地 Skill 的真实调用、实测通过与失败次数，识别闲置或连续失败技能。
          </p>
        </div>
        <span className="chip num">{skills.length} 个</span>
      </div>
      {skills.length === 0 ? (
        <p className="text-[13px] text-ink-4 py-8 text-center">
          暂无已登记 Skill，可在「接入与设置」配置 skills_dirs 或通过证据回执上报 used_skills。
        </p>
      ) : (
        <div className="divide-y divide-line-soft">
          {skills.map((sk) => {
            const isFailing = (sk.fail_count || 0) >= 3 || sk.health_state === 'failing'
            const isUnusedFlywheel =
              (sk.origin || '').startsWith('flywheel') && (sk.invoke_count || 0) === 0
            return (
              <div key={sk.skill_name} className="py-3 space-y-1">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <code className="mono text-[13px] font-semibold text-ink truncate">
                      {sk.skill_name}
                    </code>
                    <span className="chip text-[10.5px]">{sk.origin || 'manual'}</span>
                    {isFailing && (
                      <span className="px-1.5 py-0.5 rounded bg-hot-soft text-hot text-[10.5px] font-semibold">
                        ⚠️ 需检修
                      </span>
                    )}
                    {!isFailing && isUnusedFlywheel && (
                      <span className="px-1.5 py-0.5 rounded bg-amber-soft text-amber-ink text-[10.5px] font-semibold">
                        零调用
                      </span>
                    )}
                  </div>
                  <span className="num text-[11.5px] text-ink-3 shrink-0">
                    调用 {sk.invoke_count || 0} · 通过 {sk.verified_pass_count || 0} · 失败{' '}
                    {sk.fail_count || 0}
                  </span>
                </div>
                {sk.description && (
                  <p className="text-[12px] text-ink-3 line-clamp-2">{sk.description}</p>
                )}
                {sk.last_used_at && (
                  <div className="text-[11px] text-ink-4 num">
                    最近实测使用：{formatDateYMD(sk.last_used_at)} · v{sk.version || 1}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </section>

    <section className="card p-5 space-y-3">
      <div className="flex items-center justify-between gap-2 pb-2.5 border-b border-line-soft">
        <div>
          <h2 className="text-[15px] font-bold text-ink">经验卡新鲜度保鲜</h2>
          <p className="text-[12px] text-ink-4 mt-0.5">
            按 30 / 90 / 180 天分级管理复核期；任务中 real 验收传回 used_knowledge_paths 自动续期。
          </p>
        </div>
        <span className="chip num">
          待复核 {knowledgeDocs.filter((d) => d.is_expired).length} / {knowledgeDocs.length}
        </span>
      </div>
      {knowledgeDocs.length === 0 ? (
        <p className="text-[13px] text-ink-4 py-8 text-center">当前知识落点暂无文档。</p>
      ) : (
        <div className="divide-y divide-line-soft max-h-[540px] overflow-y-auto pr-1">
          {[...knowledgeDocs]
            .sort((a, b) => Number(Boolean(b.is_expired)) - Number(Boolean(a.is_expired)))
            .map((doc) => (
              <div key={doc.doc_path} className="py-2.5 space-y-1">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-[13px] font-medium text-ink truncate">
                    {doc.title}
                  </span>
                  <div className="flex items-center gap-1.5 shrink-0">
                    {doc.is_expired ? (
                      <span className="px-1.5 py-0.5 rounded bg-amber-soft text-amber-ink text-[10.5px] font-semibold">
                        ⚠️ 已过复核期
                      </span>
                    ) : (
                      <span className="px-1.5 py-0.5 rounded bg-ok-soft text-ok-ink text-[10.5px] font-medium">
                        有效
                      </span>
                    )}
                    <span className="chip text-[10.5px]">
                      {doc.freshness_tier || 'evolving'}
                    </span>
                  </div>
                </div>
                <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-ink-4 num">
                  <span className="mono truncate">{doc.doc_path}</span>
                  <span className="shrink-0">
                    曝光 {doc.surface_count ?? 0} · 助益 {doc.helped_count ?? 0}
                    {doc.valid_until ? ` · 至 ${doc.valid_until}` : ''}
                  </span>
                </div>
              </div>
            ))}
        </div>
      )}
    </section>
  </div>
)
