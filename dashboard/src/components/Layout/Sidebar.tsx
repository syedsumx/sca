import React from 'react';
import { NavLink } from 'react-router-dom';
import {
  HomeIcon,
  FolderIcon,
  ShieldExclamationIcon,
  DocumentDuplicateIcon,
  CubeIcon,
  ChartBarIcon,
  Cog6ToothIcon,
  BookOpenIcon,
  SparklesIcon,
} from '@heroicons/react/24/outline';

interface NavItem {
  name: string;
  href: string;
  icon: React.ComponentType<{ className?: string }>;
}

const navigation: NavItem[] = [
  { name: 'Dashboard', href: '/', icon: HomeIcon },
  { name: 'Projects', href: '/projects', icon: FolderIcon },
  { name: 'Issues', href: '/issues', icon: ShieldExclamationIcon },
  { name: 'Duplications', href: '/duplications', icon: DocumentDuplicateIcon },
  { name: 'Dependencies', href: '/dependencies', icon: CubeIcon },
  { name: 'Coverage', href: '/coverage', icon: ChartBarIcon },
  { name: 'Rules', href: '/rules', icon: BookOpenIcon },
  { name: 'AI Vetting', href: '/ai-vetting', icon: SparklesIcon },
  { name: 'Settings', href: '/settings', icon: Cog6ToothIcon },
];

const Sidebar: React.FC = () => {
  return (
    <div className="flex flex-col w-64 bg-white border-r border-gray-200">
      {/* Logo */}
      <div className="flex items-center h-16 px-6 border-b border-gray-200">
        <div className="flex items-center space-x-2">
          <div className="w-8 h-8 bg-primary-600 rounded-lg flex items-center justify-center">
            <span className="text-white font-bold text-lg">C</span>
          </div>
          <span className="text-xl font-bold text-gray-900">CodeScope</span>
        </div>
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-4 py-4 space-y-1 overflow-y-auto">
        {navigation.map((item) => (
          <NavLink
            key={item.name}
            to={item.href}
            className={({ isActive }) =>
              `nav-link ${isActive ? 'nav-link-active' : 'nav-link-inactive'}`
            }
          >
            <item.icon className="w-5 h-5 mr-3" />
            {item.name}
          </NavLink>
        ))}
      </nav>

      {/* Footer */}
      <div className="p-4 border-t border-gray-200">
        <div className="text-xs text-gray-500">
          <p>CodeScope v0.1.0</p>
          <p className="mt-1">Static Code Analysis</p>
        </div>
      </div>
    </div>
  );
};

export default Sidebar;
