import { Route, Routes } from 'react-router'
import ProjectList from './ProjectList'
import NewProject from './NewProject'
import ProjectDetail from './ProjectDetail'
import './nmm.css'
export default function NmmPage() {
  return (
    <div className="nmm-page">
      <Routes>
        <Route index element={<ProjectList />} />
        <Route path="new" element={<NewProject />} />
        <Route path=":projectId" element={<ProjectDetail />} />
      </Routes>
    </div>
  )
}
