import React, { useState } from 'react'
import { CodeBlock, CopyButton } from '../CodeBlock'
import { PillTabs } from '../PageLayout'
import type { ConnectPayload } from '../../types'

interface Props {
  connectData: ConnectPayload | null
}

/** Tab 1 of ConnectPolicyPage: AI client connection snippets and MCP capability list. */
export const ConnectClientsTab: React.FC<Props> = ({ connectData }) => {
  const [selectedClient, setSelectedClient] = useState('codex')
  const clientObj =
    connectData?.guides.clients.find((c) => c.id === selectedClient) ||
    connectData?.guides.clients[0]
  const mcpTools = connectData?.mcp_tools || []

  return (
    <div key="tab-connect" className="animate-tab-enter max-w-3xl space-y-5">
      {!connectData ? (
        <div className="space-y-4" aria-busy="true">
          <div className="card p-4">
            <div className="skeleton-block h-10 w-full" />
          </div>
          <div className="card p-5 space-y-3">
            <div className="skeleton-block h-5 w-40" />
            <div className="skeleton-block h-28 w-full" />
          </div>
        </div>
      ) : (
        <>
          <div className="card p-4 flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="text-[12px] text-ink-4">本地服务地址（MCP）</div>
              <code className="mono text-[13.5px] font-medium text-ink">
                {connectData.guides.endpoints.mcp_http_url}
              </code>
            </div>
            <CopyButton text={connectData.guides.endpoints.mcp_http_url} label="复制地址" />
          </div>

          <PillTabs
            items={connectData.guides.clients.map((c) => ({ key: c.id, label: c.name }))}
            active={selectedClient}
            onChange={setSelectedClient}
          />

          {clientObj && (
            <div key={clientObj.id} className="animate-tab-enter space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="text-[16px] font-bold text-ink">{clientObj.name} 配置方法</h2>
                <span className="chip">{clientObj.loop_guarantee}</span>
              </div>
              <p className="text-[13px] text-ink-3">{clientObj.notes}</p>
              <CodeBlock title={`第 1 步 · ${clientObj.mcp_label}`} code={clientObj.mcp_config} />
              <CodeBlock title={`第 2 步 · ${clientObj.hook_label}`} code={clientObj.hook_config} />
            </div>
          )}

          {mcpTools.length > 0 && (
            <section className="card p-5">
              <h3 className="text-[14.5px] font-bold text-ink mb-3">
                连上后，AI 工具会多出这 {mcpTools.length} 个能力
              </h3>
              <ul className="space-y-2 text-[13px] leading-[1.7] text-ink-2">
                {mcpTools.map((tool) => (
                  <li key={tool.name} className="flex gap-2">
                    <span className="mt-[9px] size-1 shrink-0 rounded-full bg-accent" />
                    <span>
                      <strong className="font-semibold text-ink">{tool.title_zh}</strong>（
                      <code className="mono text-[11.5px] text-ink-3">{tool.name}</code>
                      ）：{tool.description_zh || tool.description}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </div>
  )
}
