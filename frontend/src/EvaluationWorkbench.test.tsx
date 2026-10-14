import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from './App';
import { parseDataset } from './evaluationApi';
const metric = { hit_rate: 1, recall: 1, mrr: 1, ndcg: 1, no_answer_accuracy: null, positive_cases: 1, no_answer_cases: 0 };
const hit = { key: 'technique:1', type: 'technique', id: 1, title: '示例打磨', excerpt: '测试摘要', rank: 1, score: 2, reasons: ['编号命中'], source_ids: [], sources: [] };
const dataset = { id: 'demo-v1', name: '人工示例集', description: '人工回归样例', corpus: 'demo', case_count: 1, builtin: true, cases: [{ id: 'q1', query: '打磨', category: '素组', judgments: [{ key: 'technique:1', grade: 3 }] }] };
const report = { id: 1, dataset_id: 'demo-v1', dataset_name: '人工示例集', created_at: '2026-10-10', k: 5, corpus: 'demo', corpus_size: 1, dataset_hash: 'abc', corpus_hash: 'def', versions: { baseline: 'v2-lexical', ranked: 'v3' }, metrics: { baseline: metric, ranked: metric }, cases: [{ ...dataset.cases[0], baseline: [hit], ranked: [hit], metrics: { baseline: metric, ranked: metric } }] };
const reply = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }));
let saved: any;
let fail = false;
let history: any[] = [];
beforeEach(() => {
    saved = null;
    fail = false;
    history = [];
    window.history.replaceState(null, '', '/#evaluation');
    vi.stubGlobal('fetch', vi.fn((input: any, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith('/evaluations/datasets')) {
            if (init?.method === 'POST') {
                saved = { ...JSON.parse(String(init.body)), id: 'custom', builtin: false, corpus: 'live', case_count: 1 };
                return reply(saved);
            }
            return reply(saved ? [dataset, saved] : [dataset]);
        }
        if (url.endsWith('/evaluations/datasets/demo-v1'))
            return reply(dataset);
        if (url.endsWith('/evaluations/datasets/custom')) {
            if (init?.method === 'PUT') {
                saved = { ...saved, ...JSON.parse(String(init.body)) };
            }
            return reply(saved);
        }
        if (url.endsWith('/evaluations/runs'))
            return init?.method === 'POST' ? reply(fail ? { detail: '正例已撤回' } : report, fail ? 422 : 200) : reply(history);
        if (/\/evaluations\/runs\/\d+$/.test(url))
            return reply({ ...report, id: Number(url.split('/').at(-1)) });
        if (url.includes('/retrieval/preview'))
            return reply({ query: '未知', k: 5, corpus_size: 0, version: 'v3', baseline: [], ranked: [] });
        if (url.endsWith('/techniques'))
            return reply([{ id: 1, title: '真实打磨', status: 'published', visibility: 'private', category: '素组', source_ids: [1], steps: [], tags: [] }]);
        if (url.endsWith('/sources'))
            return reply([{ id: 1, title: '说明书', status: 'published' }]);
        return reply([]);
    }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
async function sets() { const user = userEvent.setup(); render(<App />); await user.click(await screen.findByRole('button', { name: '测评集' })); await screen.findByRole('option', { name: /人工示例集/ }); return user; }
it('runs builtin, explains null metrics, and never opens a real record from synthetic hits', async () => { const user = await sets(); await user.click(screen.getByRole('button', { name: '运行测评' })); expect(await screen.findByRole('heading', { name: /报告 #1/ })).toBeInTheDocument(); expect(screen.getByText(/人工编写的合成示例/)).toBeInTheDocument(); expect(screen.getAllByText(/不适用/).length).toBeGreaterThan(0); await user.click(screen.getByText('打磨', { selector: 'summary' })); expect(screen.queryByRole('button', { name: /示例打磨/ })).not.toBeInTheDocument(); expect(screen.getAllByText('编号命中').length).toBeGreaterThan(0); });
it('shows no matches and actionable server errors', async () => { const user = userEvent.setup(); render(<App />); await user.type(await screen.findByLabelText('检索问题'), '未知'); await user.click(screen.getByRole('button', { name: '对比检索' })); expect(await screen.findAllByText('没有匹配资料')).toHaveLength(2); await user.click(screen.getByRole('button', { name: '测评集' })); fail = true; await user.click(screen.getByRole('button', { name: '运行测评' })); expect(await screen.findByRole('alert')).toHaveTextContent('正例已撤回'); });
it('imports live JSON as editable draft, saves and edits custom set with explicit no-answer', async () => { const user = await sets(); await user.click(screen.getByRole('button', { name: '新建测评集' })); fireEvent.change(screen.getByLabelText('导入 JSON 内容'), { target: { value: JSON.stringify({ name: '自定义', description: '', cases: [{ id: 'a', query: '没有答案', category: '边界', judgments: [] }] }) } }); await user.click(screen.getByRole('button', { name: '载入 JSON 草稿' })); expect(screen.getByLabelText('测评集名称')).toHaveValue('自定义'); expect(screen.getByText(/预期无答案/)).toBeInTheDocument(); await user.click(screen.getByRole('button', { name: '保存测评集' })); await screen.findByText('测评集已保存'); await user.click(screen.getByRole('button', { name: '编辑测评集' })); await user.clear(screen.getByLabelText('测评集名称')); await user.type(screen.getByLabelText('测评集名称'), '修订集'); await user.click(screen.getByRole('button', { name: '保存测评集' })); await waitFor(() => expect(screen.getByRole('option', { name: /修订集/ })).toBeInTheDocument()); });
it('rejects malformed and synthetic JSON without losing editor draft', async () => { const user = await sets(); await user.click(screen.getByRole('button', { name: '新建测评集' })); fireEvent.change(screen.getByLabelText('导入 JSON 内容'), { target: { value: JSON.stringify(dataset) } }); await user.click(screen.getByRole('button', { name: '载入 JSON 草稿' })); expect(await screen.findByRole('alert')).toHaveTextContent('示例语料'); fireEvent.change(screen.getByLabelText('导入 JSON 内容'), { target: { value: '{"name":"bad","cases":[]}' } }); await user.click(screen.getByRole('button', { name: '载入 JSON 草稿' })); expect(await screen.findByRole('alert')).toHaveTextContent('1–200'); });
it('ignores history detail response arriving after a newer selection', async () => { history = [{ ...report, id: 1 }, { ...report, id: 2 }]; const user = await sets(); const original = fetch; let release: (value: Response) => void = () => { }; vi.stubGlobal('fetch', vi.fn((input: any, init?: RequestInit) => String(input).endsWith('/evaluations/runs/1') ? new Promise<Response>(resolve => { release = resolve; }) : original(input, init))); await user.click(screen.getByRole('button', { name: /查看报告 #1/ })); await user.click(screen.getByRole('button', { name: /查看报告 #2/ })); expect(await screen.findByRole('heading', { name: /报告 #2 ·/ })).toBeInTheDocument(); await act(async () => { release(new Response(JSON.stringify(report))); }); expect(screen.getByRole('heading', { name: /报告 #2 ·/ })).toBeInTheDocument(); expect(screen.queryByRole('heading', { name: /报告 #1 ·/ })).not.toBeInTheDocument(); });
it('keeps globally ranked product before techniques in library search', async () => { window.history.replaceState(null, '', '/'); const original = fetch; vi.stubGlobal('fetch', vi.fn((input: any, init?: RequestInit) => String(input).includes('/search?q=') ? reply({ techniques: [{ id: 1, title: '真实打磨', status: 'published', category: '素组', source_ids: [1], steps: [], tags: [] }], products: [{ id: 2, brand: '品牌', name: '精确产品', status: 'published', source_ids: [1], aliases: [] }], ranking: ['product:2', 'technique:1'] }) : original(input, init))); const user = userEvent.setup(); render(<App />); await user.type(screen.getByLabelText('搜索资料'), '产品'); await user.click(screen.getByRole('button', { name: '搜索' })); await screen.findByRole('button', { name: /精确产品/ }); const cards = screen.getByRole('region', { name: '资料列表' }).querySelectorAll('.result-card'); expect(cards[0]).toHaveTextContent('精确产品'); expect(screen.getByText('按相关性显示前 20 条结果')).toBeInTheDocument(); });
it('builds custom positive judgments with editable grades and confirms deletion', async () => { const user = await sets(); await user.click(screen.getByRole('button', { name: '新建测评集' })); await user.type(screen.getByLabelText('测评集名称'), '资料测评'); await user.type(screen.getByLabelText('问题 1'), '打磨'); await user.selectOptions(screen.getByLabelText('添加正例 1'), 'technique:1'); await user.selectOptions(screen.getByLabelText('第 1 题 technique:1 相关等级'), '2'); await user.click(screen.getByRole('button', { name: '保存测评集' })); await screen.findByText('测评集已保存'); await user.click(screen.getByRole('button', { name: '编辑测评集' })); expect(await screen.findByLabelText('第 1 题 technique:1 相关等级')).toHaveValue('2'); await user.click(screen.getByRole('button', { name: '取消编辑' })); vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true); await user.click(screen.getByRole('button', { name: '删除测评集' })); expect(screen.getByRole('option', { name: /资料测评/ })).toBeInTheDocument(); await user.click(screen.getByRole('button', { name: '删除测评集' })); expect(await screen.findByText('测评集已删除')).toBeInTheDocument(); expect(screen.queryByRole('option', { name: /资料测评/ })).not.toBeInTheDocument(); });
it('includes API-shaped private techniques and public products in the positive record picker', async () => { const original = fetch; vi.stubGlobal('fetch', vi.fn((input: any, init?: RequestInit) => String(input).endsWith('/products') ? reply([{ id: 2, brand: '田宫', name: '底漆', status: 'published', visibility: 'public', owner_id: 'local', source_ids: [1], aliases: [] }]) : original(input, init))); const user = await sets(); await user.click(screen.getByRole('button', { name: '新建测评集' })); expect(await screen.findByRole('option', { name: '真实打磨（technique:1）' })).toBeInTheDocument(); expect(screen.getByRole('option', { name: '田宫 底漆（product:2）' })).toBeInTheDocument(); await user.selectOptions(screen.getByLabelText('添加正例 1'), 'technique:1'); await user.selectOptions(screen.getByLabelText('添加正例 1'), 'product:2'); expect(screen.getByLabelText('第 1 题 technique:1 相关等级')).toBeInTheDocument(); expect(screen.getByLabelText('第 1 题 product:2 相关等级')).toBeInTheDocument(); });
it('imports and persists empty-query API datasets and labels blank report queries', async () => {
    const user = await sets();
    await user.click(screen.getByRole('button', {name:'新建测评集'}));
    fireEvent.change(screen.getByLabelText('导入 JSON 内容'), {target:{value:JSON.stringify({id:'from-export',name:'空查询回归',description:'',builtin:false,corpus:'live',case_count:2,cases:[{id:'empty',query:'',category:'边界',judgments:[]},{id:'spaces',query:'   ',category:'边界',judgments:[]}]})}});
    await user.click(screen.getByRole('button', {name:'载入 JSON 草稿'}));
    expect(screen.getByLabelText('测评集名称')).toHaveValue('空查询回归');
    await user.click(screen.getByRole('button', {name:'保存测评集'}));
    await screen.findByText('测评集已保存');
    await user.click(screen.getByRole('button', {name:'编辑测评集'}));
    expect(await screen.findByLabelText('问题 1')).toHaveValue('');
    expect(screen.getByLabelText('问题 2')).toHaveValue('   ');
    await user.click(screen.getByRole('button', {name:'取消编辑'}));
    const original = fetch;
    vi.stubGlobal('fetch', vi.fn((input:any, init?:RequestInit) => String(input).endsWith('/evaluations/runs') && init?.method === 'POST' ? reply({...report,cases:[{...report.cases[0],query:'   '}]}) : original(input,init)));
    await user.click(screen.getByRole('button', {name:'运行测评'}));
    expect(await screen.findByText('空查询', {selector:'summary'})).toBeInTheDocument();
});

it('exports a near-limit live dataset as compact importable JSON without server metadata', async () => {
    const input = {name:'boundary',cases:Array.from({length:200},(_,i)=>({id:`q${i}`,query:'涂'.repeat(1700),category:'test',judgments:[]})),description:''};
    const targetBytes = 1024 * 1024 - 1;
    const initialBytes = new Blob([JSON.stringify(input)]).size;
    const extraCharacters = Math.ceil((targetBytes - initialBytes - 4000) / 3);
    input.cases.forEach((c,i)=>{c.query += '涂'.repeat(Math.floor(extraCharacters / 200) + (i < extraCharacters % 200 ? 1 : 0));});
    input.description = 'x'.repeat(targetBytes - new Blob([JSON.stringify(input)]).size);
    expect(input.cases).toHaveLength(200);
    expect(input.cases.every(c=>c.query.length <= 2000 && c.id.length <= 100 && c.category.length <= 100)).toBe(true);
    expect(input.description.length).toBeLessThanOrEqual(4000);
    expect(input.name.length).toBeLessThanOrEqual(200);
    expect(new Blob([JSON.stringify(input)]).size).toBe(targetBytes);
    saved = {...input,id:'custom',corpus:'live',builtin:false,case_count:200};
    let download: Blob | undefined;
    vi.stubGlobal('URL', class extends URL { static createObjectURL(blob:Blob) { download=blob; return 'blob:test'; } static revokeObjectURL() {} });
    vi.spyOn(HTMLAnchorElement.prototype,'click').mockImplementation(()=>{});
    const user=await sets();
    await user.selectOptions(screen.getByLabelText('选择测评集'),'custom');
    await user.click(screen.getByRole('button',{name:'导出 JSON'}));
    await waitFor(()=>expect(download).toBeDefined());
    const raw=await new Promise<string>((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result));reader.onerror=reject;reader.readAsText(download!);});
    expect(()=>parseDataset(raw)).not.toThrow();
    expect(JSON.parse(raw)).toEqual(input);
});
