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
  ArrowRightOnRectangleIcon,
  UserCircleIcon,
} from '@heroicons/react/24/outline';
import { useAuth } from '../../contexts/AuthContext';

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
  const { user, logout } = useAuth();

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

      {/* User & Logout */}
      <div className="p-4 border-t border-gray-200">
        {user && (
          <div className="flex items-center justify-between">
            <div className="flex items-center min-w-0">
              <UserCircleIcon className="w-8 h-8 text-gray-400 flex-shrink-0" />
              <div className="ml-2 min-w-0">
                <p className="text-sm font-medium text-gray-900 truncate">
                  {user.display_name || user.username}
                </p>
                <p className="text-xs text-gray-500 truncate">{user.role}</p>
              </div>
            </div>
            <button
              onClick={logout}
              className="p-1.5 text-gray-400 hover:text-red-600 rounded-md hover:bg-gray-100"
              title="Sign out"
            >
              <ArrowRightOnRectangleIcon className="w-5 h-5" />
            </button>
          </div>
        )}
        <div className="text-xs text-gray-500 mt-3">
          <p>CodeScope v0.1.0</p>
        </div>
      </div>
    </div>
  );
};

export default Sidebar;
