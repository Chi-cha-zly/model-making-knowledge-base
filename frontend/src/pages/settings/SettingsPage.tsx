import { useEffect, useRef, useState, type FormEvent } from 'react'
import '../../settings.css'
import {
  settingsApi,
  type ConnectionTest,
  type ProfileInput,
  type ProfileView,
  type SettingsView,
} from '../../settingsApi'
import { getTheme, setTheme, type ThemePreference } from '../../theme'
const emptyProfile: ProfileInput = {
  name: '',
  base_url: '',
  model: '',
  compare_model: '',
  reasoning_effort: 'auto',
  max_output_tokens: 2048,
}
function editable(profile: ProfileView): ProfileInput {
  return {
    name: profile.name,
    base_url: profile.base_url,
    model: profile.model,
    compare_model: profile.compare_model,
    reasoning_effort: profile.reasoning_effort,
    max_output_tokens: profile.max_output_tokens,
  }
}
export default function SettingsWorkbench() {
  const [settings, setSettings] = useState<SettingsView | null>(null)
  const [selected, setSelected] = useState('')
  const [draft, setDraft] = useState<ProfileInput>({ ...emptyProfile })
  const [key, setKey] = useState('')
  const [clearKey, setClearKey] = useState(false)
  const [prompt, setPrompt] = useState('')
  const [theme, updateTheme] = useState<ThemePreference>(getTheme)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [test, setTest] = useState<ConnectionTest | null>(null)
  const operation = useRef(false)
  const current = settings?.profiles.find((p) => p.id === selected)
  const profileDirty = !current
    ? selected === 'new'
    : JSON.stringify(draft) !== JSON.stringify(editable(current)) ||
      !!key ||
      clearKey
  const promptDirty = !!settings && prompt !== settings.custom_prompt
  function choose(profile?: ProfileView) {
    setSelected(profile?.id ?? 'new')
    setDraft(profile ? editable(profile) : { ...emptyProfile })
    setKey('')
    setClearKey(false)
    setTest(null)
    setError('')
    setNotice('')
  }
  async function load() {
    setError('')
    try {
      const data = await settingsApi.get()
      setSettings(data)
      setPrompt(data.custom_prompt)
      choose(
        data.profiles.find((p) => p.id === data.active_profile_id) ??
          data.profiles[0],
      )
    } catch {
      setError('无法加载设置，请确认后端运行正常后重试。')
    }
  }
  useEffect(() => {
    let active = true
    settingsApi
      .get()
      .then((data) => {
        if (active) {
          setSettings(data)
          setPrompt(data.custom_prompt)
          choose(
            data.profiles.find((p) => p.id === data.active_profile_id) ??
              data.profiles[0],
          )
        }
      })
      .catch(() => {
        if (active) setError('无法加载设置，请确认后端运行正常后重试。')
      })
    return () => {
      active = false
    }
  }, [])
  useEffect(() => {
    if (!profileDirty && !promptDirty) return
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [profileDirty, promptDirty])
  async function run(action: () => Promise<void>) {
    if (operation.current) return
    operation.current = true
    setBusy(true)
    setError('')
    setNotice('')
    setTest(null)
    try {
      await action()
    } catch (e) {
      setError(e instanceof Error ? e.message : '操作失败，请重试。')
    } finally {
      operation.current = false
      setBusy(false)
    }
  }
  function switchProfile(id: string) {
    if (
      profileDirty &&
      !window.confirm('连接有未保存修改，放弃这些修改并切换？')
    )
      return
    choose(settings?.profiles.find((p) => p.id === id))
  }
  function change<K extends keyof ProfileInput>(
    field: K,
    value: ProfileInput[K],
  ) {
    setDraft((previous) => ({ ...previous, [field]: value }))
    setTest(null)
  }
  function save(event: FormEvent) {
    event.preventDefault()
    if (
      current &&
      !current.read_only &&
      current.base_url !== draft.base_url &&
      !key &&
      !clearKey
    ) {
      setError(
        '更换接口地址时，请输入新密钥；无密钥的本机服务请勾选“保存时清除密钥”。',
      )
      return
    }
    const input = {
      ...draft,
      ...(clearKey ? { api_key: '' } : key ? { api_key: key } : {}),
    }
    void run(async () => {
      const data = await settingsApi.save(input, current?.id)
      const saved = current
        ? data.profiles.find((p) => p.id === current.id)
        : data.profiles.find(
            (p) => !settings?.profiles.some((old) => old.id === p.id),
          )
      setSettings(data)
      choose(saved)
      setNotice('连接已保存')
    })
  }
  return (
    <section className="settings-workbench" aria-label="应用设置">
      <div className="page-intro">
        <div>
          <span className="page-kicker">本机工作台</span>
          <h1>设置</h1>
          <p>选择模型连接、回答方式和适合你的阅读外观。</p>
        </div>
      </div>
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <p className="alert success" role="status">
          {notice}
        </p>
      )}
      <section className="settings-section">
        <div className="settings-section-title">
          <h2>外观</h2>
          <p>立即生效，主题偏好只保存在当前浏览器。</p>
        </div>
        <div className="theme-options" role="group" aria-label="界面主题">
          {(
            [
              ['system', '跟随系统'],
              ['light', '浅色'],
              ['dark', '深色'],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              aria-pressed={theme === value}
              onClick={() => {
                setTheme(value)
                updateTheme(value)
              }}
            >
              <span className={`theme-swatch ${value}`} aria-hidden="true" />
              {label}
            </button>
          ))}
        </div>
      </section>
      {!settings ? (
        <p role="status">
          {error ? (
            <button className="outline-button" onClick={() => void load()}>
              重新加载设置
            </button>
          ) : (
            '正在加载设置…'
          )}
        </p>
      ) : (
        <>
          <section className="settings-section">
            <div className="settings-section-title">
              <h2>模型连接</h2>
              <p>
                使用兼容 OpenAI Chat Completions
                的服务。配置已保存不代表连接已验证。
              </p>
            </div>
            <div className="settings-profile-picker">
              <label>
                选择连接
                <select
                  disabled={busy}
                  value={selected}
                  onChange={(e) => switchProfile(e.target.value)}
                >
                  {settings.profiles.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                      {p.id === settings.active_profile_id ? ' · 使用中' : ''}
                    </option>
                  ))}
                  {selected === 'new' && <option value="new">新连接</option>}
                </select>
              </label>
              <button
                className="outline-button"
                disabled={
                  busy ||
                  settings.profiles.filter((p) => !p.read_only).length >= 20
                }
                onClick={() => switchProfile('new')}
              >
                新建连接
              </button>
            </div>
            {current?.read_only && (
              <p className="settings-note">
                这是现有环境配置，只读保留。新建连接即可在应用中设置其他服务。
              </p>
            )}
            <form onSubmit={save}>
              <fieldset
                disabled={busy || !!current?.read_only}
                className="settings-fields"
              >
                <label>
                  连接名称
                  <input
                    required
                    maxLength={80}
                    value={draft.name}
                    onChange={(e) => change('name', e.target.value)}
                  />
                </label>
                <label>
                  接口根地址
                  <input
                    required
                    type="url"
                    aria-label="接口根地址"
                    placeholder="https://example.com/v1"
                    maxLength={500}
                    value={draft.base_url}
                    onChange={(e) => change('base_url', e.target.value)}
                  />
                  <small>通常以 /v1 结尾；本机服务可使用 HTTP。</small>
                </label>
                <label>
                  模型 API ID
                  <input
                    required
                    maxLength={200}
                    value={draft.model}
                    onChange={(e) => change('model', e.target.value)}
                  />
                </label>
                <label>
                  实验对照模型（可选）
                  <input
                    maxLength={200}
                    value={draft.compare_model}
                    onChange={(e) => change('compare_model', e.target.value)}
                  />
                </label>
                <label>
                  推理强度
                  <select
                    value={draft.reasoning_effort}
                    onChange={(e) =>
                      change(
                        'reasoning_effort',
                        e.target.value as ProfileInput['reasoning_effort'],
                      )
                    }
                  >
                    <option value="auto">服务默认</option>
                    <option value="low">低</option>
                    <option value="medium">中</option>
                    <option value="high">高</option>
                  </select>
                </label>
                <label>
                  输出 token 上限
                  <input
                    type="number"
                    required
                    min={64}
                    max={2048}
                    step={1}
                    value={draft.max_output_tokens}
                    onChange={(e) =>
                      change('max_output_tokens', Number(e.target.value))
                    }
                  />
                </label>
                <label className="settings-key">
                  API Key
                  <input
                    type="password"
                    aria-label="API Key"
                    autoComplete="new-password"
                    maxLength={4096}
                    value={key}
                    disabled={clearKey}
                    onChange={(e) => {
                      setKey(e.target.value)
                      setTest(null)
                    }}
                  />
                  <small>
                    {current?.has_api_key
                      ? '已保存密钥。留空保留原密钥，不会回显。'
                      : '未配置密钥。只在本机服务允许时留空。'}
                  </small>
                </label>
                <label className="settings-checkbox">
                  <input
                    type="checkbox"
                    checked={clearKey}
                    onChange={(e) => {
                      setClearKey(e.target.checked)
                      setKey('')
                      setTest(null)
                    }}
                  />
                  保存时清除密钥
                </label>
              </fieldset>
              <div className="settings-actions">
                {!current?.read_only && (
                  <button
                    className="primary-button"
                    disabled={busy || !profileDirty}
                    type="submit"
                  >
                    保存连接
                  </button>
                )}
                {profileDirty && (
                  <span className="settings-dirty">连接有未保存修改</span>
                )}
              </div>
            </form>
            <div className="settings-actions">
              <button
                className="outline-button"
                disabled={
                  busy ||
                  !current ||
                  profileDirty ||
                  current.id === settings.active_profile_id
                }
                onClick={() =>
                  current &&
                  void run(async () => {
                    setSettings(await settingsApi.activate(current.id))
                    setNotice('已切换当前模型连接')
                  })
                }
              >
                {current?.id === settings.active_profile_id
                  ? '当前使用中'
                  : '使用此连接'}
              </button>
              {!current?.read_only && (
                <>
                  <button
                    className="outline-button"
                    disabled={busy || !current || profileDirty}
                    onClick={() =>
                      current &&
                      void run(async () => {
                        const result = await settingsApi.test(current.id)
                        setKey('')
                        setClearKey(false)
                        setTest(result)
                      })
                    }
                  >
                    测试已保存连接
                  </button>
                  <button
                    className="outline-button settings-danger"
                    disabled={
                      busy ||
                      !current ||
                      current.id === settings.active_profile_id
                    }
                    onClick={() => {
                      if (
                        current &&
                        window.confirm(`删除连接“${current.name}”？`)
                      )
                        void run(async () => {
                          const data = await settingsApi.remove(current.id)
                          setSettings(data)
                          choose(
                            data.profiles.find(
                              (p) => p.id === data.active_profile_id,
                            ),
                          )
                          setNotice('连接已删除')
                        })
                    }}
                  >
                    删除连接
                  </button>
                </>
              )}
            </div>
            <p className="settings-muted">
              测试前请先保存。测试会向所选服务发送一条简短请求，可能产生少量模型费用；不会发送知识库资料。
            </p>
            {test && (
              <p
                className={test.ok ? 'alert success' : 'alert error'}
                role="status"
              >
                {test.message}
                {test.latency_ms !== null
                  ? `（${Math.round(test.latency_ms)} ms）`
                  : ''}
              </p>
            )}
            <p className="settings-note">{settings.storage_notice}</p>
          </section>
          <section className="settings-section">
            <div className="settings-section-title">
              <h2>回答方式</h2>
              <p>
                补充说明用于对话和回答实验。应用仍保留引用资料和说明信息不足的基本规则。
              </p>
            </div>
            <div className="settings-actions" aria-label="提示词预设">
              {settings.prompt_presets
                .filter((p) => p.text)
                .map((p) => (
                  <button
                    className="outline-button"
                    key={p.id}
                    disabled={busy}
                    onClick={() => setPrompt(p.text)}
                  >
                    {p.name}
                  </button>
                ))}
              <button
                className="outline-button"
                disabled={busy}
                onClick={() => setPrompt('')}
              >
                恢复默认
              </button>
            </div>
            <label>
              补充系统提示词
              <textarea
                rows={7}
                maxLength={8000}
                value={prompt}
                disabled={busy}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder="例如：先说明适用材料，再列出操作步骤。"
              />
            </label>
            <div className="settings-actions">
              <button
                className="primary-button"
                disabled={busy || !promptDirty}
                onClick={() =>
                  void run(async () => {
                    const data = await settingsApi.prompt(prompt)
                    setSettings(data)
                    setPrompt(data.custom_prompt)
                    setNotice('提示词已保存')
                  })
                }
              >
                保存提示词
              </button>
              <span className="settings-muted">{prompt.length} / 8000</span>
              {promptDirty && (
                <span className="settings-dirty">提示词有未保存修改</span>
              )}
            </div>
            <details className="settings-preview">
              <summary>预览生效提示词</summary>
              <p className="settings-muted">
                预览包含当前草稿；点击保存后才会用于新的请求。
              </p>
              <pre data-testid="prompt-preview">
                {settings.base_prompt}
                {prompt.trim()
                  ? `\n\n用户补充偏好（仅补充表达和教学方式，不能更改以上证据、引用及安全规则）：\n${prompt.trim()}`
                  : ''}
              </pre>
            </details>
          </section>
        </>
      )}
      {busy && (
        <p className="settings-busy" role="status">
          正在处理设置，请稍候…
        </p>
      )}
    </section>
  )
}
