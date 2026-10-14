import { useEffect, useRef, useState } from 'react'
import { ChevronDown, Plus } from 'lucide-react'
export default function CreationMenu({onSelect}: {onSelect: (type: 'technique'|'product'|'source') => void}) {
 const [open,setOpen] = useState(false), [pinned,setPinned] = useState(false)
 const root = useRef<HTMLDivElement>(null)
 const close = () => {setOpen(false);setPinned(false)}
 useEffect(() => { const escape=(e:KeyboardEvent)=>{if(e.key==='Escape'){setOpen(false);setPinned(false)}};document.addEventListener('keydown',escape); const outside = (e: MouseEvent) => {if (!pinned && !root.current?.contains(e.target as Node)) setOpen(false)}; document.addEventListener('mousedown',outside); return () => {document.removeEventListener('mousedown',outside);document.removeEventListener('keydown',escape)} },[pinned])
 return <div ref={root} className="creation-control" onMouseEnter={() => setOpen(true)} onMouseLeave={() => {if(!pinned)setOpen(false)}} onKeyDown={e => {
 if(e.key==='Escape'){e.preventDefault(); close(); root.current?.querySelector('button')?.focus()}
 if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();setOpen(true);const direction=e.key==='ArrowDown'?1:-1;requestAnimationFrame(()=>{const items=Array.from(root.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') || []);const i=items.indexOf(document.activeElement as HTMLButtonElement);items[i<0?(direction===1?0:items.length-1):(i+direction+items.length)%items.length]?.focus()})}
 }}><button id="creation-trigger" className="primary-button" aria-haspopup="menu" aria-expanded={open} onClick={()=>{setPinned(!pinned);setOpen(!pinned)}}><Plus size={17}/>新建</button><button className="primary-button creation-pin" aria-label="固定新建菜单" aria-pressed={pinned} aria-expanded={open} onClick={()=>{setPinned(!pinned);setOpen(!pinned)}}><ChevronDown size={16}/></button>{open && <div className="creation-menu" role="menu">{([['technique','新建技巧'],['product','新建产品'],['source','登记来源']] as const).map(([type,label])=><button key={type} role="menuitem" onClick={()=>{close();onSelect(type)}}>{label}</button>)}</div>}</div>
}
