import type { ContentBlock } from './api'
export default function ArticleBody({blocks}: {blocks: ContentBlock[]}) {
 return <div className="article-body">{blocks.map((block,i)=>block.type==='image'?<figure key={i}><img src={`/api/v1/attachments/${block.attachment_id}`} alt={block.caption || '制作过程图片（若未显示，请检查附件）'}/>{block.caption && <figcaption>{block.caption}</figcaption>}</figure>:block.type==='heading'?<h3 key={i}>{block.text}</h3>:<p key={i}>{block.text}</p>)}</div>
}
