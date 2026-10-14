import type { RecordType, Source } from './api';
export type Judgment = {
    key: string;
    grade: 1 | 2 | 3;
};
export type EvaluationCase = {
    id: string;
    query: string;
    category: string;
    judgments: Judgment[];
};
export type DatasetInput = {
    name: string;
    description: string;
    cases: EvaluationCase[];
};
export type Dataset = {
    id: string;
    name: string;
    description: string;
    corpus: 'demo' | 'live';
    case_count: number;
    builtin: boolean;
    cases?: EvaluationCase[];
};
export type Hit = {
    key: string;
    type: RecordType;
    id: number;
    title: string;
    excerpt: string;
    rank: number;
    score: number;
    reasons: string[];
    source_ids: number[];
    sources: Source[];
};
export type Metrics = {
    hit_rate: number | null;
    recall: number | null;
    mrr: number | null;
    ndcg: number | null;
    no_answer_accuracy: number | null;
    positive_cases: number;
    no_answer_cases: number;
};
export type RunSummary = {
    id: number;
    dataset_id: string;
    dataset_name: string;
    created_at: string;
    k: number;
    corpus: 'demo' | 'live';
    metrics: {
        baseline: Metrics;
        ranked: Metrics;
    };
};
export type Report = RunSummary & {
    corpus_size: number;
    dataset_hash: string;
    corpus_hash: string;
    versions: {
        baseline: string;
        ranked: string;
    };
    cases: (EvaluationCase & {
        baseline: Hit[];
        ranked: Hit[];
        metrics: {
            baseline: Metrics;
            ranked: Metrics;
        };
    })[];
};
export type Preview = {
    query: string;
    k: number;
    corpus_size: number;
    version: string;
    baseline: Hit[];
    ranked: Hit[];
};
async function request<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
    const response = await fetch(`/api/v1${path}`, { method, ...(body ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}) });
    const data = await response.json().catch(() => null);
    if (!response.ok)
        throw new Error(typeof data?.detail === 'string' ? data.detail : `请求失败（${response.status}），请检查测评内容后重试`);
    return data as T;
}
export const evaluationApi = {
    preview: (q: string, k: number) => request<Preview>(`/retrieval/preview?q=${encodeURIComponent(q)}&k=${k}`),
    datasets: () => request<Dataset[]>('/evaluations/datasets'), dataset: (id: string) => request<Dataset>(`/evaluations/datasets/${encodeURIComponent(id)}`),
    save: (body: DatasetInput, id?: string) => request<Dataset>(`/evaluations/datasets${id ? `/${encodeURIComponent(id)}` : ''}`, id ? 'PUT' : 'POST', body),
    remove: (id: string) => request<{
        deleted: boolean;
    }>(`/evaluations/datasets/${encodeURIComponent(id)}`, 'DELETE'),
    run: (dataset_id: string, k: number) => request<Report>('/evaluations/runs', 'POST', { dataset_id, k }),
    history: () => request<RunSummary[]>('/evaluations/runs'), report: (id: number) => request<Report>(`/evaluations/runs/${id}`),
};
export function parseDataset(raw: string): DatasetInput {
    if (new Blob([raw]).size > 1024 * 1024)
        throw new Error('JSON 不得超过 1 MiB');
    let data: any;
    try {
        data = JSON.parse(raw);
    }
    catch {
        throw new Error('JSON 格式错误，请检查括号和引号');
    }
    if (data?.corpus === 'demo' || data?.builtin === true)
        throw new Error('示例语料不能导入真实资料库测评集，请使用 live 自定义集');
    if (typeof data?.name !== 'string' || !data.name.trim())
        throw new Error('请填写测评集名称');
    if (!Array.isArray(data.cases) || data.cases.length < 1 || data.cases.length > 200)
        throw new Error('测评集需要 1–200 道题');
    const ids = new Set<string>();
    for (const c of data.cases) {
        if (!c || typeof c.id !== 'string' || !c.id.trim() || ids.has(c.id))
            throw new Error('题目 ID 必须非空且唯一');
        ids.add(c.id);
        if (typeof c.query !== 'string' || c.query.length > 2000 || typeof c.category !== 'string')
            throw new Error('每题需要字符串问题（可为空，最多 2000 字）和分类');
        if (!Array.isArray(c.judgments))
            throw new Error('每题必须提供 judgments；空数组表示预期无答案');
        const keys = new Set<string>();
        for (const j of c.judgments) {
            if (!j || !/^((technique)|(product)):[1-9]\d*$/.test(j.key) || ![1, 2, 3].includes(j.grade) || keys.has(j.key))
                throw new Error('正例需要唯一资料编号及 1、2 或 3 级相关性');
            keys.add(j.key);
        }
    }
    return { name: data.name.trim(), description: typeof data.description === 'string' ? data.description : '', cases: data.cases.map((c: EvaluationCase) => ({ id: c.id, query: c.query, category: c.category, judgments: c.judgments.map(j => ({ key: j.key, grade: j.grade })) })) };
}
