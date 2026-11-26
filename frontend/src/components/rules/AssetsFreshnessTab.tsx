import React, { useEffect, useMemo, useState } from 'react'
import { ListToolbar, PaginationBar, usePagination } from '../ListControls'
import { DocumentReaderModal } from '../ui/DocumentReaderModal'
import { formatDateYMD, FRESHNESS_TIER_SHORT } from '../../lib/labels'
import type { KnowledgeDocSummary, SkillAssetRecord } from '../../types'

const SKILL_ORIGIN_LABELS: Record<string, string> = {
  local_probed: '本地探测',
  manual: '手动登记',
  flywheel: '飞轮沉淀',
}

export const AssetsFreshnessTab: React.FC<{
  skills: SkillAssetRecord[]
  knowledgeDocs: KnowledgeDocSummary[]
  initialDocFilter?: 'all' | 'expired' | 'fresh' | 'archived'
  initialSkillFilter?: 'all' | 'used' | 'untested' | 'failing'
  intentNonce?: number
  onChanged?: () => void
}> = ({
  skills,
  knowledgeDocs,
  initialDocFilter,
  initialSkillFilter,
  intentNonce,
  onChanged,
}) => {
  const [skillQuery, setSkillQuery] = useState('')
  const [skillFilter, setSkillFilter] = useState<'all' | 'used' | 'untested' | 'failing'>(
    initialSkillFilter || 'all'
  )
  const [docQuery, setDocQuery] = useState('')
  const [docFilter, setDocFilter] = useState<'all' | 'expired' | 'fresh' | 'archived'>(
    initialDocFilter || 'all'
  )
  const [readingDoc, setReadingDoc] = useState<KnowledgeDocSummary | null>(null)

  useEffect(() => {
    if (!intentNonce) return
    if (initialDocFilter) setDocFilter(initialDocFilter)
    if (initialSkillFilter) setSkillFilter(initialSkillFilter)
  }, [intentNonce, initialDocFilter, initialSkillFilter])

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

  const { expiredCount, freshCount, archivedCount } = useMemo(() => {
    let exp = 0
    let fr = 0
    let arc = 0
    for (const d of knowledgeDocs) {
      const isArchived = d.freshness_state === 'archived' || d.freshness_state === 'superseded'
      if (isArchived) arc++
      else if (d.is_expired) exp++
      else fr++
    }
    return { expiredCount: exp, freshCount: fr, archivedCount: arc }
  }, [knowledgeDocs])

  const filteredDocs = useMemo(() => {
    const q = docQuery.trim().toLowerCase()
    return knowledgeDocs
      .filter((d) => {
        const isArchived = d.freshness_state === 'archived' || d.freshness_state === 'superseded'
        if (docFilter === 'expired') return Boolean(d.is_expired)
        if (docFilter === 'fresh') return !d.is_expired && !isArchived
        if (docFilter === 'archived') return isArchived
        return true
      })
      .filter((d) =>
        !q
          ? true
          : `${d.title} ${d.doc_path} ${d.freshness_tier || ''}`.toLowerCase().includes(q)
      )
      .sort((a, b) => {
        const aScore = a.is_expired ? 0 : a.freshness_state === 'archived' || a.freshness_state === 'superseded' ? 2 : 1
        const bScore = b.is_expired ? 0 : b.freshness_state === 'archived' || b.freshness_state === 'superseded' ? 2 : 1
        return aScore - bScore
      })
  }, [knowledgeDocs, docQuery, docFilter])

  const skillPager = usePagination(filteredSkills, 10)
  const docPager = usePagination(filteredDocs, 10)

  return (
    <>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <section className="card p-5 space-y-3">
          <div className="flex items-center justify-between gap-2 pb-2.5 border-b border-line-soft">
            <div>
              <h2 className="text-[15px] font-bold text-ink">Skill 使用效果</h2>
              <p className="text-[12px] text-ink-4 mt-0.5">
                汇总本地 Skill 与任务实测次数，自动识别长期零调用或连续失败的技能。
              </p>
            </div>
            <span className="chip num">{skills.length} 个</span>
          </div>

          {skills.length > 0 && (
            <ListToolbar
              query={skillQuery}
              onQueryChange={(q) => {
                setSkillQuery(q)
                skillPager.resetPage()
              }}
              placeholder="搜索 Skill 名称或描述…"
              hasActiveFilters={Boolean(skillQuery.trim()) || skillFilter !== 'all'}
              onResetFilters={() => {
                setSkillQuery('')
                setSkillFilter('all')
                skillPager.resetPage()
              }}
            >
              <select
                aria-label="Skill 状态筛选"
                value={skillFilter}
                onChange={(e) => {
                  setSkillFilter(e.target.value as 'all' | 'used' | 'untested' | 'failing')
                  skillPager.resetPage()
                }}
                className="field-input h-8 px-2 text-[12px]"
              >
                <option value="all">全部</option>
                <option value="used">已实测调用</option>
                <option value="untested">待实测</option>
                <option value="failing">异常需检修</option>
              </select>
            </ListToolbar>
          )}

          {filteredSkills.length === 0 ? (
            <p className="text-[13px] text-ink-4 py-8 text-center">
              {skills.length === 0
                ? '暂无已探测的 Skill，可在「接入与设置 → 目录与模型」指定本地技能目录。'
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
                          {SKILL_ORIGIN_LABELS[sk.origin || 'manual'] || sk.origin || '手动登记'}
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
              <h2 className="text-[15px] font-bold text-ink">知识笔记保鲜期</h2>
              <p className="text-[12px] text-ink-4 mt-0.5">
                按 30 / 90 / 180 天自动管理复核期；任务中实际引用并通过实测的笔记会自动顺延。点击可阅读全文。
              </p>
            </div>
            <span className="chip num">
              待复核 {expiredCount} / {knowledgeDocs.length}
            </span>
          </div>

          {knowledgeDocs.length > 0 && (
            <ListToolbar
              query={docQuery}
              onQueryChange={(q) => {
                setDocQuery(q)
                docPager.resetPage()
              }}
              placeholder="搜索笔记标题或路径…"
              hasActiveFilters={Boolean(docQuery.trim()) || docFilter !== 'all'}
              onResetFilters={() => {
                setDocQuery('')
                setDocFilter('all')
                docPager.resetPage()
              }}
            >
              <select
                aria-label="笔记保鲜状态筛选"
                value={docFilter}
                onChange={(e) => {
                  setDocFilter(e.target.value as 'all' | 'expired' | 'fresh' | 'archived')
                  docPager.resetPage()
                }}
                className="field-input h-8 px-2 text-[12px]"
              >
                <option value="all">全部 ({knowledgeDocs.length})</option>
                <option value="expired">仅看待复核 ({expiredCount})</option>
                <option value="fresh">仅看有效 ({freshCount})</option>
                <option value="archived">仅看已归档 ({archivedCount})</option>
              </select>
            </ListToolbar>
          )}

          {filteredDocs.length === 0 ? (
            <p className="text-[13px] text-ink-4 py-8 text-center">没有符合条件的知识笔记。</p>
          ) : (
            <div className="divide-y divide-line-soft">
              {docPager.pageItems.map((doc) => {
                const tierKey = doc.freshness_tier || 'evolving'
                const tierLabel = FRESHNESS_TIER_SHORT[tierKey] || tierKey
                const isArchived =
                  doc.freshness_state === 'archived' || doc.freshness_state === 'superseded'
                return (
                  <button
                    key={doc.doc_path}
                    type="button"
                    onClick={() => setReadingDoc(doc)}
                    className="pressable w-full text-left py-2.5 px-2 -mx-2 rounded-[var(--radius-tile)] hover:bg-bg-sunk/70 space-y-1"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="text-[13px] font-medium text-ink truncate">
                        {doc.title}
                      </span>
                      <div className="flex items-center gap-1.5 shrink-0">
                        {isArchived ? (
                          <span className="px-1.5 py-0.5 rounded bg-bg-sunk text-ink-4 text-[10.5px] font-medium border border-line-soft">
                            已归档
                          </span>
                        ) : doc.is_expired ? (
                          <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-amber-soft text-amber-ink text-[10.5px] font-semibold">
                            <span className="size-1.5 rounded-full bg-amber" />
                            待复核
                          </span>
                        ) : (
                          <span className="px-1.5 py-0.5 rounded bg-ok-soft text-ok-ink text-[10.5px] font-medium">
                            有效
                          </span>
                        )}
                        <span className="chip text-[10.5px]">{tierLabel}</span>
                      </div>
                    </div>
                    <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-ink-4 num">
                      <span className="mono truncate">{doc.doc_path}</span>
                      <span className="shrink-0">
                        曝光 {doc.surface_count ?? 0} · 助益 {doc.helped_count ?? 0}
                        {doc.valid_until ? ` · 至 ${doc.valid_until}` : ''}
                      </span>
                    </div>
                  </button>
                )
              })}
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

      <DocumentReaderModal
        docPath={readingDoc?.doc_path ?? null}
        title={readingDoc?.title}
        freshness={readingDoc}
        onFreshnessUpdated={(updated) => {
          setReadingDoc(updated)
          onChanged?.()
        }}
        onClose={() => setReadingDoc(null)}
      />
    </>
  )
}
