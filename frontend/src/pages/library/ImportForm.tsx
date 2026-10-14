import { FileUp } from 'lucide-react'
import { useState } from 'react'
export default function ImportForm({
  busy,
  onImport,
}: {
  busy: boolean
  onImport: (file: File, title: string, url: string) => void
}) {
  const [file, setFile] = useState<File | null>(null)
  const [title, setTitle] = useState('')
  const [url, setUrl] = useState('')
  return (
    <form
      className="import-form"
      onSubmit={(event) => {
        event.preventDefault()
        if (file) onImport(file, title, url)
      }}
    >
      <label className="dropzone">
        <FileUp size={25} />
        <strong>{file?.name || '选择 Markdown 文件'}</strong>
        <span>支持 .md，导入后可继续修订</span>
        <input
          type="file"
          accept=".md,text/markdown,text/plain"
          onChange={(event) => setFile(event.target.files?.[0] || null)}
        />
      </label>
      <label>
        来源标题
        <input
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="例如：个人制作笔记"
        />
      </label>
      <label>
        来源链接（可选）
        <input
          type="url"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="https://"
        />
      </label>
      <button className="primary-button" disabled={!file || busy}>
        开始导入
      </button>
    </form>
  )
}
