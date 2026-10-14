import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import CreationMenu from './CreationMenu'
afterEach(cleanup)
it('opens on hover, stays pinned after leave, and closes on Escape', () => {
 render(<CreationMenu onSelect={vi.fn()} />)
 const button = screen.getByRole('button', {name:'新建'})
 fireEvent.mouseEnter(button.parentElement!)
 expect(screen.getByRole('menuitem', {name:'新建技巧'})).toBeVisible()
 fireEvent.click(screen.getByRole('button', {name:'固定新建菜单'}))
 fireEvent.mouseLeave(button.parentElement!)
 expect(screen.getByRole('menu')).toBeVisible()
 fireEvent.keyDown(button, {key:'Escape'})
 expect(screen.queryByRole('menu')).not.toBeInTheDocument()
})
it('closes a pinned menu on Escape even after focus moves outside',()=>{
 render(<><CreationMenu onSelect={vi.fn()}/><button>外部</button></>)
 fireEvent.click(screen.getByRole('button',{name:'固定新建菜单'}))
 screen.getByRole('button',{name:'外部'}).focus()
 fireEvent.keyDown(document,{key:'Escape'})
 expect(screen.queryByRole('menu')).not.toBeInTheDocument()
})
it('ArrowUp from the trigger focuses the final item and selection closes the menu',async()=>{
 const select=vi.fn()
 render(<CreationMenu onSelect={select}/>)
 fireEvent.keyDown(screen.getByRole('button',{name:'新建'}),{key:'ArrowUp'})
 await new Promise(r=>setTimeout(r,30))
 expect(screen.getByRole('menuitem',{name:'登记来源'})).toHaveFocus()
 fireEvent.click(screen.getByRole('menuitem',{name:'登记来源'}))
 expect(select).toHaveBeenCalledWith('source')
 expect(screen.queryByRole('menu')).not.toBeInTheDocument()
})
