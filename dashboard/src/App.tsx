import React from 'react';
import { Routes, Route } from 'react-router-dom';
import { MainLayout } from './components/Layout';
import Dashboard from './pages/Dashboard';
import Projects from './pages/Projects';
import Issues from './pages/Issues';
import Duplications from './pages/Duplications';
import Dependencies from './pages/Dependencies';
import Coverage from './pages/Coverage';
import Rules from './pages/Rules';
import Settings from './pages/Settings';

const App: React.FC = () => {
  return (
    <Routes>
      <Route path="/" element={<MainLayout />}>
        <Route index element={<Dashboard />} />
        <Route path="projects" element={<Projects />} />
        <Route path="projects/:projectId" element={<Dashboard />} />
        <Route path="issues" element={<Issues />} />
        <Route path="duplications" element={<Duplications />} />
        <Route path="dependencies" element={<Dependencies />} />
        <Route path="coverage" element={<Coverage />} />
        <Route path="rules" element={<Rules />} />
        <Route path="settings" element={<Settings />} />
      </Route>
    </Routes>
  );
};

export default App;
