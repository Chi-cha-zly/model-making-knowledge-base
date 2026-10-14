import {
  Archive,
  BookOpenText,
  ChevronRight,
  Download,
  FileUp,
  Layers3,
  Plus,
  Upload,
} from 'lucide-react'
import { api } from '../../api'
import ImportForm from './ImportForm'
import type { LibraryController } from './useLibrary'
export default function ManagePage({
  sources,
  techniques,
  busy,
  openEditor,
  changeTechniqueStatus,
  runAction,
  download,
}: LibraryController) {
  return (
    <section className="manage-page">
      <div className="page-intro">
        <div>
          <span className="page-kicker">资料维护</span>
          <h1>让每条记录有据可查。</h1>
          <p>管理来源、导入手稿，并保存整个资料库。</p>
        </div>
        <div className="intro-line" />
      </div>
      <div className="manage-grid">
        <section className="manage-panel">
          <div className="panel-heading">
            <FileUp size={22} />
            <h2>导入 Markdown</h2>
          </div>
          <p>上传本地 .md 文件，保留原始出处后再整理成技巧。</p>
          <ImportForm
            busy={busy}
            onImport={async (file, title, url) =>
              runAction(
                () => api.importMarkdown(file, title, url),
                'Markdown 已导入',
              )
            }
          />
        </section>
        <section className="manage-panel">
          <div className="panel-heading">
            <Archive size={22} />
            <h2>备份与恢复</h2>
          </div>
          <p>
            备份包含资料和附件。恢复会以所选备份替换当前资料，请先下载一份当前备份。
          </p>
          <button className="outline-button" disabled={busy} onClick={download}>
            <Download size={17} />
            下载备份
          </button>
          <label className="file-button">
            <Upload size={17} />从 ZIP 恢复
            <input
              aria-label="从 ZIP 恢复"
              type="file"
              accept=".zip,application/zip"
              disabled={busy}
              onChange={(event) => {
                const file = event.target.files?.[0]
                if (
                  file &&
                  window.confirm('恢复备份会替换当前资料库。确定继续吗？')
                )
                  void runAction(() => api.restoreBackup(file), '资料库已恢复')
                event.target.value = ''
              }}
            />
          </label>
        </section>
      </div>
      <div className="source-heading">
        <div>
          <h2>技巧管理</h2>
          <p>编辑已保存技巧，或恢复发布已撤回的内容。</p>
        </div>
      </div>
      {techniques.length ? (
        <div className="source-list">
          {techniques.map((item) => (
            <div className="source-row" key={item.id}>
              <div className="source-icon">
                <Layers3 size={18} />
              </div>
              <div>
                <strong>{item.title}</strong>
                <span>
                  {item.status === 'published'
                    ? '已发布'
                    : item.status === 'withdrawn'
                      ? '已撤回'
                      : '草稿'}
                  {item.source_ids.some((id) =>
                    sources.some(
                      (source) =>
                        source.id === id && source.source_type === 'original',
                    ),
                  )
                    ? ' · 个人原创'
                    : ''}
                </span>
              </div>
              <button
                className="source-action"
                aria-label={`编辑技巧${item.title}`}
                onClick={() => openEditor({ type: 'technique', id: item.id })}
              >
                编辑
              </button>
              {item.status === 'published' ? (
                <button
                  className="source-action danger"
                  disabled={busy}
                  aria-label={`撤回技巧${item.title}`}
                  onClick={() => void changeTechniqueStatus(item, false)}
                >
                  撤回
                </button>
              ) : (
                <button
                  className="source-action"
                  disabled={busy}
                  aria-label={`恢复发布${item.title}`}
                  onClick={() => void changeTechniqueStatus(item, true)}
                >
                  {item.status === 'withdrawn' ? '恢复发布' : '发布'}
                </button>
              )}
            </div>
          ))}
        </div>
      ) : (
        <div className="empty-small">尚无技巧。可直接新建个人原创技巧。</div>
      )}
      <div className="source-heading">
        <div>
          <h2>来源档案</h2>
          <p>{sources.length} 条已登记的原始资料</p>
        </div>
        <button
          className="outline-button"
          onClick={() => openEditor({ type: 'source' })}
        >
          <Plus size={17} />
          登记来源
        </button>
      </div>
      {sources.length ? (
        <div className="source-list">
          {sources.map((source) => (
            <div className="source-row" key={source.id}>
              <div className="source-icon">
                <BookOpenText size={18} />
              </div>
              <div>
                <strong>
                  {source.title}
                  {source.status === 'withdrawn' && (
                    <span className="withdrawn-label">已撤回</span>
                  )}
                </strong>
                <span>
                  {source.source_type === 'original' ? '个人原创 · ' : ''}
                  {source.author || '作者未填写'}
                  {source.license_note ? ` · ${source.license_note}` : ''}
                </span>
              </div>
              {source.url && (
                <a href={source.url} target="_blank" rel="noreferrer">
                  打开原文 <ChevronRight size={16} />
                </a>
              )}
              <button
                className="source-action"
                aria-label={`编辑${source.title}`}
                onClick={() => openEditor({ type: 'source', id: source.id })}
              >
                编辑
              </button>
              {source.status !== 'withdrawn' && (
                <button
                  className="source-action danger"
                  aria-label={`撤回${source.title}`}
                  onClick={() => {
                    if (
                      window.confirm(
                        `撤回“${source.title}”后，引用它的资料将不再出现在检索结果中。确定继续吗？`,
                      )
                    )
                      void runAction(
                        () => api.withdrawSource(source.id),
                        '来源已撤回',
                      )
                  }}
                >
                  撤回
                </button>
              )}
            </div>
          ))}
        </div>
      ) : (
        <div className="empty-small">
          尚无来源。先登记一本说明书、厂商页面或个人笔记。
        </div>
      )}
    </section>
  )
}
