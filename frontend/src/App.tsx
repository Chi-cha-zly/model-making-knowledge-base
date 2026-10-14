import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  Archive,
  BookOpenText,
  Check,
  ChevronRight,
  Layers3,
  Menu,
  MessageSquare,
  Search,
  Settings2,
  X,
} from 'lucide-react'
import { useState } from 'react'
import {
  HashRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from 'react-router'
import type { RecordType } from './api'
import ChatWorkbench from './pages/chat/ChatPage'
import CreationMenu from './CreationMenu'
import EvaluationWorkbench from './pages/evaluation/EvaluationPage'
import BrowsePage from './pages/library/BrowsePage'
import EditorOverlay from './pages/library/EditorOverlay'
import ManagePage from './pages/library/ManagePage'
import { formatError, sourceIsPublished } from './pages/library/types'
import { useLibrary } from './pages/library/useLibrary'
import SettingsWorkbench from './pages/settings/SettingsPage'
import TechniqueEditor from './TechniqueEditor'

export default function App() {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            retry: false,
            staleTime: 30_000,
            refetchOnWindowFocus: false,
          },
        },
      }),
  )
  return (
    <QueryClientProvider client={client}>
      <HashRouter>
        <Workbench />
      </HashRouter>
    </QueryClientProvider>
  )
}

function Workbench() {
  const location = useLocation()
  const navigate = useNavigate()
  const mode = location.pathname.startsWith('/settings')
    ? 'settings'
    : location.pathname.startsWith('/evaluation')
      ? 'evaluation'
      : location.pathname.startsWith('/chat')
        ? 'chat'
        : location.pathname === '/manage'
          ? 'manage'
          : 'browse'
  const setMode = (next: string) => {
    void navigate(next === 'browse' ? '/' : `/${next}`)
  }
  const library = useLibrary(mode, setMode)
  const {
    sources,
    techniques,
    products,
    selected,
    editor,
    setEditor,
    setSelected,
    error,
    setError,
    notice,
    setNotice,
    busy,
    setBusy,
    openEditor,
    openEntry,
    saveTechnique,
    saveProduct,
    saveSource,
  } = library
  const [mobileMenu, setMobileMenu] = useState(false)
  function openReference(citation: { type: RecordType; id: number }) {
    const item =
      citation.type === 'technique'
        ? techniques.find((item) => item.id === citation.id)
        : products.find((item) => item.id === citation.id)
    if (
      item &&
      item.status === 'published' &&
      item.source_ids.some((id) =>
        sources.some((source) => source.id === id && sourceIsPublished(source)),
      )
    ) {
      setMode('browse')
      openEntry({ type: citation.type, item })
    } else setError('这条资料已不可用。历史记录保留运行时的引用快照。')
  }
  return (
    <div className="app-shell">
      <aside className={`sidebar ${mobileMenu ? 'mobile-open' : ''}`}>
        <div className="brand">
          <div className="brand-mark">
            <Layers3 size={24} strokeWidth={1.7} />
          </div>
          <div>
            <strong>模型制作</strong>
            <span>知识库 / WORKBENCH</span>
          </div>
        </div>
        <nav aria-label="主导航" className="main-nav">
          <button
            className={mode === 'evaluation' ? 'active' : ''}
            onClick={() => {
              setMode('evaluation')
              setEditor(null)
              setSelected(null)
              setMobileMenu(false)
            }}
          >
            <Search size={19} />
            检索测评
          </button>
          <button
            className={mode === 'chat' ? 'active' : ''}
            onClick={() => {
              setMode('chat')
              setEditor(null)
              setSelected(null)
              setMobileMenu(false)
            }}
          >
            <MessageSquare size={19} />
            对话助手
          </button>
          <button
            className={mode === 'browse' ? 'active' : ''}
            onClick={() => {
              setMode('browse')
              setMobileMenu(false)
            }}
          >
            <BookOpenText size={19} />
            资料检索
          </button>
          <button
            className={mode === 'manage' ? 'active' : ''}
            onClick={() => {
              setMode('manage')
              setEditor(null)
              setSelected(null)
              setMobileMenu(false)
            }}
          >
            <Archive size={19} />
            资料管理
          </button>
          <button
            className={mode === 'settings' ? 'active' : ''}
            onClick={() => {
              setMode('settings')
              setEditor(null)
              setSelected(null)
              setMobileMenu(false)
            }}
          >
            <Settings2 size={19} />
            设置
          </button>
        </nav>
        <div className="sidebar-bottom">
          <div className="sidebar-note">
            <span className="pin" />
            本机资料库
            <br />
            <small>每条记录都应有可核对的来源</small>
          </div>
          <span className="sidebar-version">V6 / PERSONAL WORKBENCH</span>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <button
            className="mobile-toggle icon-button"
            aria-label="打开菜单"
            onClick={() => setMobileMenu(!mobileMenu)}
          >
            <Menu size={22} />
          </button>
          <div className="breadcrumb">
            知识库 <ChevronRight size={14} />{' '}
            {mode === 'settings'
              ? '设置'
              : mode === 'evaluation'
                ? '检索测评'
                : mode === 'chat'
                  ? '对话助手'
                  : mode === 'browse'
                    ? '资料检索'
                    : '资料管理'}
          </div>
          <div className="topbar-actions">
            <CreationMenu onSelect={(type) => openEditor({ type })} />
          </div>
        </header>
        <main>
          {error && (
            <div className="alert error" role="alert">
              <span>{error}</span>
              <button aria-label="关闭错误" onClick={() => setError('')}>
                <X size={16} />
              </button>
            </div>
          )}
          {notice && (
            <div className="alert success" role="status">
              <Check size={17} />
              <span>{notice}</span>
              <button aria-label="关闭通知" onClick={() => setNotice('')}>
                <X size={16} />
              </button>
            </div>
          )}
          <Routes>
            <Route path="settings" element={<SettingsWorkbench />} />
            <Route
              path="evaluation/*"
              element={<EvaluationWorkbench onOpenReference={openReference} />}
            />
            <Route
              path="evaluation-experiments"
              element={<EvaluationWorkbench onOpenReference={openReference} />}
            />
            <Route
              path="chat/*"
              element={<ChatWorkbench onOpenReference={openReference} />}
            />
            <Route path="manage" element={<ManagePage {...library} />} />
            <Route path="*" element={<BrowsePage {...library} />} />
          </Routes>
        </main>
      </div>
      {editor?.type === 'technique' && (
        <TechniqueEditor
          key={editor.id || 'new'}
          technique={techniques.find((t) => t.id === editor.id)}
          sources={sources}
          onClose={() => setEditor(null)}
          onSave={saveTechnique}
        />
      )}
      {editor && editor.type !== 'technique' && (
        <EditorOverlay
          editor={editor}
          sources={sources}
          selected={selected}
          techniques={techniques}
          products={products}
          onClose={() => setEditor(null)}
          onSaveProduct={async (values) => {
            setBusy(true)
            setError('')
            try {
              await saveProduct(values)
            } catch (err) {
              setError(formatError(err))
            } finally {
              setBusy(false)
            }
          }}
          onSaveSource={async (values) => {
            setBusy(true)
            setError('')
            try {
              await saveSource(values)
            } catch (err) {
              setError(formatError(err))
            } finally {
              setBusy(false)
            }
          }}
          busy={busy}
          error={error}
          onNewSource={() => setEditor({ type: 'source' })}
        />
      )}
    </div>
  )
}
