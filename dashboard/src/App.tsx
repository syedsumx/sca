import React from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { MainLayout } from './components/Layout';
import { AuthProvider, useAuth } from './contexts/AuthContext';
import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import Projects from './pages/Projects';
import Issues from './pages/Issues';
import Duplications from './pages/Duplications';
import Dependencies from './pages/Dependencies';
import Coverage from './pages/Coverage';
import Rules from './pages/Rules';
import AIVetting from './pages/AIVetting';
import Trends from './pages/Trends';
import Secrets from './pages/Secrets';
import SBOM from './pages/SBOM';
import CICD from './pages/CICD';
import Compare from './pages/Compare';
import Settings from './pages/Settings';

const ProtectedRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { isAuthenticated, isLoading } = useAuth();

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50">
        <div className="text-gray-500">Loading...</div>
      </div>
    );
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  return <>{children}</>;
};

const AppRoutes: React.FC = () => {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        path="/"
        element={
          <ProtectedRoute>
            <MainLayout />
          </ProtectedRoute>
        }
      >
        <Route index element={<Dashboard />} />
        <Route path="projects" element={<Projects />} />
        <Route path="projects/:projectId" element={<Dashboard />} />
        <Route path="issues" element={<Issues />} />
        <Route path="duplications" element={<Duplications />} />
        <Route path="dependencies" element={<Dependencies />} />
        <Route path="coverage" element={<Coverage />} />
        <Route path="trends" element={<Trends />} />
        <Route path="secrets" element={<Secrets />} />
        <Route path="sbom" element={<SBOM />} />
        <Route path="cicd" element={<CICD />} />
        <Route path="compare" element={<Compare />} />
        <Route path="rules" element={<Rules />} />
        <Route path="ai-vetting" element={<AIVetting />} />
        <Route path="settings" element={<Settings />} />
      </Route>
    </Routes>
  );
};

const App: React.FC = () => {
  return (
    <AuthProvider>
      <AppRoutes />
    </AuthProvider>
  );
};

export default App;
