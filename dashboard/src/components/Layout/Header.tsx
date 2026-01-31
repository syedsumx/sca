import React from 'react';
import {
  BellIcon,
  MagnifyingGlassIcon,
  UserCircleIcon,
} from '@heroicons/react/24/outline';

interface HeaderProps {
  title?: string;
}

const Header: React.FC<HeaderProps> = ({ title }) => {
  return (
    <header className="bg-white border-b border-gray-200">
      <div className="flex items-center justify-between h-16 px-6">
        {/* Page Title */}
        <div className="flex items-center">
          {title && (
            <h1 className="text-xl font-semibold text-gray-900">{title}</h1>
          )}
        </div>

        {/* Right side */}
        <div className="flex items-center space-x-4">
          {/* Search */}
          <div className="relative">
            <MagnifyingGlassIcon className="absolute left-3 top-1/2 transform -translate-y-1/2 w-5 h-5 text-gray-400" />
            <input
              type="text"
              placeholder="Search..."
              className="pl-10 pr-4 py-2 w-64 text-sm border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent"
            />
          </div>

          {/* Notifications */}
          <button className="relative p-2 text-gray-400 hover:text-gray-500 hover:bg-gray-100 rounded-lg">
            <BellIcon className="w-6 h-6" />
            <span className="absolute top-1 right-1 w-2 h-2 bg-red-500 rounded-full"></span>
          </button>

          {/* User menu */}
          <button className="flex items-center space-x-2 p-2 hover:bg-gray-100 rounded-lg">
            <UserCircleIcon className="w-8 h-8 text-gray-400" />
            <span className="text-sm font-medium text-gray-700">Admin</span>
          </button>
        </div>
      </div>
    </header>
  );
};

export default Header;
