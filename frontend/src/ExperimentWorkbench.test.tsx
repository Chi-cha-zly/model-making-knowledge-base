import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import EvaluationWorkbench from './EvaluationWorkbench';
const reply = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), {status}));
const settings = {provider:'langchain',model:'gpt-6.1-sol',model_configured:true,compare_models:['gpt-6-luna'],reasoning_effort:'low',max_output_tokens:2048,langsmith:{configured:true,tracing_enabled:false,project:'model-making',endpoint:'https://api.smith.langchain.com',message:'配置已读取'}};
const dataset = {id:'demo',name:'示例集',description:'',corpus:'demo',case_count:12,builtin:true};
const job = {id:1,dataset_id:'demo',dataset_name:'示例集',corpus:'demo',mode:'retrieval',models:[],status:'running',progress:0,total:6,created_at:'2026-10-10',updated_at:'2026-10-10',error:null,links:[],results:[]};
let configured: boolean, jobs: typeof job[], posts: any[], stopCount: number, details: number;
beforeEach(()=>{
  configured=true; jobs=[]; posts=[]; stopCount=0; details=0; window.history.replaceState(null,'','/');
  vi.stubGlobal('fetch',vi.fn((url: any, init?: RequestInit)=>{
    const path=String(url);
    if(path.endsWith('/integrations/status')) return reply({...settings,langsmith:{...settings.langsmith,configured}});
    if(path.endsWith('/evaluations/datasets')) return reply([{...dataset,id:'live',name:'真实集',corpus:'live'},dataset]);
    if(path.endsWith('/evaluations/runs')) return reply([]);
    if(path.endsWith('/experiments/1/stop')) {stopCount++; return reply({...job,status:'stopped'});}
    if(path.endsWith('/experiments/1')) {details++; return reply({...job,progress:2});}
    if(path.endsWith('/experiments')) {
      if(init?.method==='POST') {posts.push(JSON.parse(String(init.body))); return reply(job,202);}
      return reply(jobs);
    }
    throw new Error(`Unexpected ${path}`);
  }));
});
afterEach(()=>{cleanup();vi.useRealTimers();vi.unstubAllGlobals();vi.restoreAllMocks();});
async function open() { const user=userEvent.setup(); render(<EvaluationWorkbench onOpenReference={()=>{}}/>); await user.click(screen.getByRole('button',{name:'LangSmith 实验'})); await screen.findByRole('heading',{name:'LangSmith 实验'}); return user; }
it('blocks cloud execution without a key and explains configuration is not connectivity',async()=>{
  configured=false; await open();
  expect(await screen.findByText(/LANGSMITH_API_KEY/)).toBeInTheDocument();
  expect(screen.getByRole('button',{name:'开始云端实验'})).toBeDisabled();
  expect(screen.getByText(/不代表连接已验证/)).toBeInTheDocument();
  expect(screen.queryByLabelText(/密钥/)).not.toBeInTheDocument();
  expect(posts).toHaveLength(0);
});
it('defaults to demo retrieval, caps sample controls, starts a bounded job and stops it',async()=>{
  const user=await open(); await screen.findByText(/配置已读取/);
  expect(screen.getByLabelText('实验测评集')).toHaveValue('demo');
  expect(screen.getByLabelText('抽样题数')).toHaveValue('3');
  expect(screen.getByLabelText('抽样题数').querySelectorAll('option')).toHaveLength(10);
  expect(screen.getByText(/问题、标签、检索片段和实验结果/)).toBeInTheDocument();
  await user.click(screen.getByRole('button',{name:'开始云端实验'}));
  await screen.findByRole('heading',{name:/实验 #1/});
  expect(posts[0]).toMatchObject({dataset_id:'demo',mode:'retrieval',limit:3,k:5,models:[]});
  expect(posts[0].request_id).toBeTruthy();
  expect(screen.getByRole('button',{name:'开始云端实验'})).toBeDisabled();
  await user.click(screen.getByRole('button',{name:'停止实验'}));
  expect(await screen.findByText('已停止')).toBeInTheDocument(); expect(stopCount).toBe(1);
});
it('requires a model in answer mode and labels estimates and mechanical checks honestly',async()=>{
  const user=await open(); await screen.findByText(/配置已读取/);
  await user.selectOptions(screen.getByLabelText('实验模式'),'answers');
  expect(screen.getByText(/额外产生模型调用费用/)).toBeInTheDocument();
  expect(screen.getByText(/不代表事实正确率/)).toBeInTheDocument();
  expect(screen.getByText(/估算.*账单/)).toBeInTheDocument();
  await user.click(screen.getByLabelText('gpt-6.1-sol'));
  expect(screen.getByRole('button',{name:'开始云端实验'})).toBeDisabled();
  await user.click(screen.getByLabelText('gpt-6-luna'));
  await user.click(screen.getByRole('button',{name:'开始云端实验'}));
  await waitFor(()=>expect(posts).toHaveLength(1));
  expect(posts[0]).toMatchObject({mode:'answers',models:['gpt-6-luna']});
});
it('resumes an active history job, polls progress, and stops polling when unmounted',async()=>{
  jobs=[job]; await open(); await screen.findByRole('heading',{name:/实验 #1/});
  await waitFor(()=>expect(details).toBeGreaterThan(0));
  expect(screen.getByRole('progressbar')).toHaveAttribute('value','2');
  vi.useFakeTimers(); cleanup(); const before=details;
  await act(async()=>{await vi.advanceTimersByTimeAsync(6000);}); expect(details).toBe(before);
});
it('ignores a slow history response after a newer report selection',async()=>{
  jobs=[{...job,id:2,status:'completed'}, {...job,status:'completed'}];
  const original=fetch; let release: (response: Response)=>void=()=>{};
  vi.stubGlobal('fetch',vi.fn((url:any,init?:RequestInit)=>String(url).endsWith('/experiments/1') ? new Promise<Response>(resolve=>{release=resolve;}) : String(url).endsWith('/experiments/2') ? reply({...job,id:2,status:'completed'}) : original(url,init)));
  const user=await open(); await screen.findByRole('button',{name:/查看实验 #1/});
  await user.click(screen.getByRole('button',{name:/查看实验 #1/}));
  await user.click(screen.getByRole('button',{name:/查看实验 #2/}));
  expect(await screen.findByRole('heading',{name:/实验 #2/})).toBeInTheDocument();
  await act(async()=>release(new Response(JSON.stringify({...job,status:'completed'}))));
  expect(screen.getByRole('heading',{name:/实验 #2/})).toBeInTheDocument();
});
it('shows backend failures and permits retry after an unsuccessful start',async()=>{
  const original=fetch;
  vi.stubGlobal('fetch',vi.fn((url:any,init?:RequestInit)=>String(url).endsWith('/experiments') && init?.method==='POST' ? reply({detail:'云端连接失败，请检查配置'},503) : original(url,init)));
  const user=await open(); await screen.findByText(/配置已读取/);
  await user.click(screen.getByRole('button',{name:'开始云端实验'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('云端连接失败');
  expect(screen.getByRole('button',{name:'开始云端实验'})).toBeEnabled();
});
it('renders result costs, nullable metrics and safe cloud links',async()=>{
  jobs=[{...job,status:'completed'}]; const original=fetch;
  vi.stubGlobal('fetch',vi.fn((url:any,init?:RequestInit)=>String(url).endsWith('/experiments/1') ? reply({...job,status:'completed',results:[{variant:'gpt-6-luna',case_id:'q1',query:'打磨',answer:'建议核对来源',hit_keys:['technique:1'],metrics:{citation_validity:null,answer_nonempty:1},usage:{input_tokens:10,output_tokens:20,total_tokens:30},latency_ms:125,cost_usd:0.000011,error:null}],links:[{label:'实验链接',url:'https://smith.langchain.com/o/test'},{label:'恶意链接',url:'javascript:alert(1)'}]}) : original(url,init)));
  const user=await open(); await user.click(await screen.findByRole('button',{name:/查看实验 #1/}));
  await screen.findByText('打磨'); fireEvent.click(screen.getByText('打磨'));
  expect(screen.getByText('建议核对来源')).toBeInTheDocument();
  expect(screen.getByText(/0.000011/)).toBeInTheDocument();
  expect(screen.getByRole('link',{name:'实验链接'})).toHaveAttribute('href','https://smith.langchain.com/o/test');
  expect(screen.queryByRole('link',{name:'恶意链接'})).not.toBeInTheDocument();
});
it('restores the cloud tab after a page refresh without starting another experiment',async()=>{
  window.history.replaceState(null,'','/#evaluation-experiments'); jobs=[job];
  render(<EvaluationWorkbench onOpenReference={()=>{}}/>);
  expect(await screen.findByRole('heading',{name:'LangSmith 实验'})).toBeInTheDocument();
  expect(await screen.findByRole('heading',{name:/实验 #1/})).toBeInTheDocument();
  expect(posts).toHaveLength(0);
});
it('does not let an in-flight poll overwrite stopped status',async()=>{
  jobs=[job]; const original=fetch; let release:(r:Response)=>void=()=>{};
  vi.stubGlobal('fetch',vi.fn((url:any,init?:RequestInit)=>String(url).endsWith('/experiments/1') ? new Promise<Response>(resolve=>{release=resolve;}) : original(url,init)));
  const user=await open(); await screen.findByRole('heading',{name:/实验 #1/});
  await user.click(screen.getByRole('button',{name:'停止实验'}));
  await screen.findByText('已停止');
  await act(async()=>release(new Response(JSON.stringify({...job,progress:1}))));
  expect(screen.getByText('已停止')).toBeInTheDocument();
  expect(screen.queryByRole('button',{name:'停止实验'})).not.toBeInTheDocument();
});
it('reuses a request id when a start response is lost so retry cannot duplicate work',async()=>{
  const original=fetch; const ids:string[]=[];
  vi.stubGlobal('fetch',vi.fn((url:any,init?:RequestInit)=>{
    if(String(url).endsWith('/experiments') && init?.method==='POST') {
      ids.push(JSON.parse(String(init.body)).request_id);
      return ids.length===1 ? Promise.reject(new Error('连接中断')) : reply(job,202);
    }
    return original(url,init);
  }));
  const user=await open(); await screen.findByText(/配置已读取/);
  await user.click(screen.getByRole('button',{name:'开始云端实验'}));
  await screen.findByRole('alert');
  await user.click(screen.getByRole('button',{name:'开始云端实验'}));
  await screen.findByRole('heading',{name:/实验 #1/});
  expect(ids).toHaveLength(2); expect(ids[0]).toBe(ids[1]);
});
it('keeps polling after a stop request fails while an older poll is pending',async()=>{
  jobs=[job]; const original=fetch; let release:(r:Response)=>void=()=>{}; let count=0;
  vi.stubGlobal('fetch',vi.fn((url:any,init?:RequestInit)=>{
    if(String(url).endsWith('/experiments/1/stop'))return reply({detail:'停止暂时失败'},503);
    if(String(url).endsWith('/experiments/1')) {count++;return count===1 ? new Promise<Response>(resolve=>{release=resolve;}) : reply({...job,progress:4});}
    return original(url,init);
  }));
  const user=await open(); await screen.findByRole('heading',{name:/实验 #1/});
  await user.click(screen.getByRole('button',{name:'停止实验'})); await screen.findByRole('alert');
  vi.useFakeTimers();
  await act(async()=>release(new Response(JSON.stringify({...job,progress:1}))));
  await act(async()=>{await vi.advanceTimersByTimeAsync(1600);});
  expect(screen.getByRole('progressbar')).toHaveAttribute('value','4');
});
