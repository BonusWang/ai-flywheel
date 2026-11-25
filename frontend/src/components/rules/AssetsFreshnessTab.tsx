import React, { useMemo, useState } from 'react'
import { ListToolbar, PaginationBar, usePagination } from '../ListControls'
import { formatDateYMD } from '../../lib/labels'
import type { KnowledgeDocSummary, SkillAssetRecord } from '../../types'

const SKILL_ORIGIN_LABELS: Record<string, string> = {
  local_probed: '本地探测',
  manual: '手动登记',
  flywheel: '飞轮沉淀',
}

/**
 * The "Skill 与保鲜" tab: the Skill effectiveness ledger (merged with locally probed
 * skills from capabilities.skills_dirs) and the knowledge card freshness ledger.
 */
export const AssetsFreshnessTab: React.FC<{
  skills: SkillAssetRecord[]
  knowledgeDocs: KnowledgeDocSummary[]
}> = ({ skills, knowledgeDocs }) => {
  const [skillQuery, setSkillQuery] = useState('')
  const [skillFilter, setSkillFilter] = useState<'all' | 'used' | 'untested' | 'failing'>('all')
  const [docQuery, setDocQuery] = useState('')
  const [docFilter, setDocFilter] = useState<'all' | 'expired' | 'fresh'>('all')

  const filteredSkills = useMemo(() => {
    const q = skillQuery.trim().toLowerCase()
    return skills
      .filter((sk) => {
        const isFailing = (sk.fail_count || 0) >= 3 || sk.health_state === 'failing'
        const inv = sk.invoke_count || 0
        if (skillFilter === 'used') return inv > 0
        if (skillFilter === 'untested') return inv === 0
        if (skillFilter === 'failing') return isFailing
        return true
      })
      .filter((sk) =>
        !q
          ? true
          : `${sk.skill_name} ${sk.description || ''} ${sk.origin || ''}`.toLowerCase().includes(q)
      )
  }, [skills, skillQuery, skillFilter])

  const expiredCount = useMemo(
    () => knowledgeDocs.filter((d) => d.is_expired).length,
    [knowledgeDocs]
  )

  const filteredDocs = useMemo(() => {
    const q = docQuery.trim().toLowerCase()
    return knowledgeDocs
      .filter((d) => {
        if (docFilter === 'expired') return Boolean(d.is_expired)
        if (docFilter === 'fresh') return !d.is_expired
        return true
      })
      .filter((d) =>
        !q
          ? true
          : `${d.title} ${d.doc_path} ${d.freshness_tier || ''}`.toLowerCase().includes(q)
      )
      .sort((a, b) => Number(Boolean(b.is_expired)) - Number(Boolean(a.is_expired)))
  }, [knowledgeDocs, docQuery, docFilter])

  const skillPager = usePagination(filteredSkills, 10)
  const docPager = usePagination(filteredDocs, 10)

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      <section className="card p-5 space-y-3">
        <div className="flex items-center justify-between gap-2 pb-2.5 border-b border-line-soft">
          <div>
            <h2 className="text-[15px] font-bold text-ink">Skill 实效账本（反 Skill 表演）</h2>
            <p className="text-[12px] text-ink-4 mt-0.5">
              自动合并本地探测 Skill 与证据回执实测次数，识别零调用或连续失败技能。
            </p>
          </div>
          <span className="chip num">{skills.length} 个</span>
        </div>

        <ListToolbar
          query={skillQuery}
          onQueryChange={(q) => {
            setSkillQuery(q)
            skillPager.resetPage()
          }}
          placeholder="搜索 Skill 名称或描述…"
          sortHint=""
        >
          <select
            aria-label="Skill 状态筛选"
            value={skillFilter}
            onChange={(e) => {
              setSkillFilter(e.target.value as 'all' | 'used' | 'untested' | 'failing')
              skillPager.resetPage()
            }}
            className="h-8 rounded-[var(--radius-control)] border border-line bg-surface px-2 text-[12px] text-ink"
          >
            <option value="all">全部</option>
            <option value="used">已实测调用</option>
            <option value="untested">待实测</option>
            <option value="failing">异常需检修</option>
          </select>
        </ListToolbar>

        {filteredSkills.length === 0 ? (
          <p className="text-[13px] text-ink-4 py-8 text-center">
            {skills.length === 0
              ? '暂无已探测或已登记 Skill，可在「接入与设置」配置 skills_dirs 或通过证据回执上报 used_skills。'
              : '没有匹配当前筛选条件的 Skill。'}
          </p>
        ) : (
          <div className="divide-y divide-line-soft">
            {skillPager.pageItems.map((sk) => {
              const isFailing = (sk.fail_count || 0) >= 3 || sk.health_state === 'failing'
              const isUnusedFlywheel =
                (sk.origin || '').startsWith('flywheel') && (sk.invoke_count || 0) === 0
              return (
                <div key={sk.skill_name} className="py-2.5 space-y-1">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-1.5 min-w-0">
                      <code className="mono text-[13px] font-semibold text-ink truncate">
                        {sk.skill_name}
                      </code>
                      <span className="chip text-[10.5px]">
                        {SKILL_ORIGIN_LABELS[sk.origin || 'manual'] || sk.origin || 'manual'}
                      </span>
                      {isFailing && (
                        <span className="px-1.5 py-0.5 rounded bg-hot-soft text-hot text-[10.5px] font-semibold">
                          需检修
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
                    <p className="text-[12px] text-ink-3 line-clamp-1">{sk.description}</p>
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

        <PaginationBar
          total={skillPager.total}
          page={skillPager.page}
          pageSize={skillPager.pageSize}
          totalPages={skillPager.totalPages}
          onPageChange={skillPager.setPage}
          onPageSizeChange={skillPager.setPageSize}
        />
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
            待复核 {expiredCount} / {knowledgeDocs.length}
          </span>
        </div>

        <ListToolbar
          query={docQuery}
          onQueryChange={(q) => {
            setDocQuery(q)
            docPager.resetPage()
          }}
          placeholder="搜索经验卡标题或路径…"
          sortHint=""
        >
          <select
            aria-label="经验卡保鲜状态筛选"
            value={docFilter}
            onChange={(e) => {
              setDocFilter(e.target.value as 'all' | 'expired' | 'fresh')
              docPager.resetPage()
            }}
            className="h-8 rounded-[var(--radius-control)] border border-line bg-surface px-2 text-[12px] text-ink"
          >
            <option value="all">全部</option>
            <option value="expired">仅看待复核 ({expiredCount})</option>
            <option value="fresh">仅看有效</option>
          </select>
        </ListToolbar>

        {filteredDocs.length === 0 ? (
          <p className="text-[13px] text-ink-4 py-8 text-center">没有符合条件的经验卡。</p>
        ) : (
          <div className="divide-y divide-line-soft">
            {docPager.pageItems.map((doc) => (
              <div key={doc.doc_path} className="py-2.5 space-y-1">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-[13px] font-medium text-ink truncate">{doc.title}</span>
                  <div className="flex items-center gap-1.5 shrink-0">
                    {doc.is_expired ? (
                      <span className="px-1.5 py-0.5 rounded bg-amber-soft text-amber-ink text-[10.5px] font-semibold">
                        已过复核期
                      </span>
                    ) : (
                      <span className="px-1.5 py-0.5 rounded bg-ok-soft text-ok-ink text-[10.5px] font-medium">
                        有效
                      </span>
                    )}
                    <span className="chip text-[10.5px]">{doc.freshness_tier || 'evolving'}</span>
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

        <PaginationBar
          total={docPager.total}
          page={docPager.page}
          pageSize={docPager.pageSize}
          totalPages={docPager.totalPages}
          onPageChange={docPager.setPage}
          onPageSizeChange={docPager.setPageSize}
        />
      </section>
    </div>
  )
}
