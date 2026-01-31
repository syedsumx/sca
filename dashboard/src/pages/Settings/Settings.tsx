import React, { useState } from 'react';
import {
  Cog6ToothIcon,
  BellIcon,
  ShieldCheckIcon,
  ServerIcon,
  PaintBrushIcon,
} from '@heroicons/react/24/outline';
import { Card, CardHeader, CardBody } from '../../components/common';

interface SettingSection {
  id: string;
  name: string;
  icon: React.ComponentType<{ className?: string }>;
}

const sections: SettingSection[] = [
  { id: 'general', name: 'General', icon: Cog6ToothIcon },
  { id: 'notifications', name: 'Notifications', icon: BellIcon },
  { id: 'quality-gates', name: 'Quality Gates', icon: ShieldCheckIcon },
  { id: 'integrations', name: 'Integrations', icon: ServerIcon },
  { id: 'appearance', name: 'Appearance', icon: PaintBrushIcon },
];

const Settings: React.FC = () => {
  const [activeSection, setActiveSection] = useState('general');

  // Settings state
  const [settings, setSettings] = useState({
    projectName: 'My Project',
    analysisTimeout: 300,
    maxFileSize: 10,
    excludePatterns: 'node_modules/**\n.git/**\ndist/**\nbuild/**',

    emailNotifications: true,
    slackNotifications: false,
    notifyOnFailure: true,
    notifyOnNewIssues: true,

    coverageThreshold: 80,
    duplicationThreshold: 3,
    maxBlockers: 0,
    maxCritical: 0,

    githubIntegration: false,
    gitlabIntegration: false,
    jiraIntegration: false,
    slackWebhook: '',

    theme: 'light',
    compactMode: false,
    showLineNumbers: true,
  });

  const handleChange = (key: string, value: unknown) => {
    setSettings((prev) => ({ ...prev, [key]: value }));
  };

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Settings</h1>
        <p className="mt-1 text-sm text-gray-500">
          Configure CodeScope analysis and preferences
        </p>
      </div>

      <div className="flex flex-col lg:flex-row gap-6">
        {/* Sidebar */}
        <div className="lg:w-64 flex-shrink-0">
          <Card>
            <CardBody className="p-2">
              <nav className="space-y-1">
                {sections.map((section) => (
                  <button
                    key={section.id}
                    onClick={() => setActiveSection(section.id)}
                    className={`w-full flex items-center px-3 py-2 text-sm font-medium rounded-md transition-colors ${
                      activeSection === section.id
                        ? 'bg-primary-100 text-primary-700'
                        : 'text-gray-600 hover:bg-gray-100'
                    }`}
                  >
                    <section.icon className="w-5 h-5 mr-3" />
                    {section.name}
                  </button>
                ))}
              </nav>
            </CardBody>
          </Card>
        </div>

        {/* Content */}
        <div className="flex-1">
          {activeSection === 'general' && (
            <Card>
              <CardHeader title="General Settings" subtitle="Basic configuration for your project" />
              <CardBody className="space-y-6">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Project Name
                  </label>
                  <input
                    type="text"
                    value={settings.projectName}
                    onChange={(e) => handleChange('projectName', e.target.value)}
                    className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Analysis Timeout (seconds)
                  </label>
                  <input
                    type="number"
                    value={settings.analysisTimeout}
                    onChange={(e) => handleChange('analysisTimeout', parseInt(e.target.value))}
                    className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
                  />
                  <p className="mt-1 text-xs text-gray-500">Maximum time for analysis before timeout</p>
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Max File Size (MB)
                  </label>
                  <input
                    type="number"
                    value={settings.maxFileSize}
                    onChange={(e) => handleChange('maxFileSize', parseInt(e.target.value))}
                    className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
                  />
                  <p className="mt-1 text-xs text-gray-500">Files larger than this will be skipped</p>
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Exclude Patterns
                  </label>
                  <textarea
                    value={settings.excludePatterns}
                    onChange={(e) => handleChange('excludePatterns', e.target.value)}
                    rows={4}
                    className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 font-mono text-sm"
                  />
                  <p className="mt-1 text-xs text-gray-500">One pattern per line, using glob syntax</p>
                </div>
              </CardBody>
            </Card>
          )}

          {activeSection === 'notifications' && (
            <Card>
              <CardHeader title="Notifications" subtitle="Configure how you receive alerts" />
              <CardBody className="space-y-6">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm font-medium text-gray-700">Email Notifications</p>
                    <p className="text-xs text-gray-500">Receive analysis results via email</p>
                  </div>
                  <button
                    onClick={() => handleChange('emailNotifications', !settings.emailNotifications)}
                    className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                      settings.emailNotifications ? 'bg-primary-600' : 'bg-gray-200'
                    }`}
                  >
                    <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                      settings.emailNotifications ? 'translate-x-6' : 'translate-x-1'
                    }`} />
                  </button>
                </div>

                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm font-medium text-gray-700">Slack Notifications</p>
                    <p className="text-xs text-gray-500">Send alerts to Slack channel</p>
                  </div>
                  <button
                    onClick={() => handleChange('slackNotifications', !settings.slackNotifications)}
                    className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                      settings.slackNotifications ? 'bg-primary-600' : 'bg-gray-200'
                    }`}
                  >
                    <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                      settings.slackNotifications ? 'translate-x-6' : 'translate-x-1'
                    }`} />
                  </button>
                </div>

                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm font-medium text-gray-700">Notify on Quality Gate Failure</p>
                    <p className="text-xs text-gray-500">Alert when quality gate fails</p>
                  </div>
                  <button
                    onClick={() => handleChange('notifyOnFailure', !settings.notifyOnFailure)}
                    className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                      settings.notifyOnFailure ? 'bg-primary-600' : 'bg-gray-200'
                    }`}
                  >
                    <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                      settings.notifyOnFailure ? 'translate-x-6' : 'translate-x-1'
                    }`} />
                  </button>
                </div>

                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm font-medium text-gray-700">Notify on New Issues</p>
                    <p className="text-xs text-gray-500">Alert when new issues are detected</p>
                  </div>
                  <button
                    onClick={() => handleChange('notifyOnNewIssues', !settings.notifyOnNewIssues)}
                    className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                      settings.notifyOnNewIssues ? 'bg-primary-600' : 'bg-gray-200'
                    }`}
                  >
                    <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                      settings.notifyOnNewIssues ? 'translate-x-6' : 'translate-x-1'
                    }`} />
                  </button>
                </div>
              </CardBody>
            </Card>
          )}

          {activeSection === 'quality-gates' && (
            <Card>
              <CardHeader title="Quality Gates" subtitle="Define thresholds for quality gate pass/fail" />
              <CardBody className="space-y-6">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Minimum Coverage (%)
                  </label>
                  <input
                    type="number"
                    value={settings.coverageThreshold}
                    onChange={(e) => handleChange('coverageThreshold', parseInt(e.target.value))}
                    min={0}
                    max={100}
                    className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Maximum Duplication (%)
                  </label>
                  <input
                    type="number"
                    value={settings.duplicationThreshold}
                    onChange={(e) => handleChange('duplicationThreshold', parseInt(e.target.value))}
                    min={0}
                    max={100}
                    className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Maximum Blocker Issues
                  </label>
                  <input
                    type="number"
                    value={settings.maxBlockers}
                    onChange={(e) => handleChange('maxBlockers', parseInt(e.target.value))}
                    min={0}
                    className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Maximum Critical Issues
                  </label>
                  <input
                    type="number"
                    value={settings.maxCritical}
                    onChange={(e) => handleChange('maxCritical', parseInt(e.target.value))}
                    min={0}
                    className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
                  />
                </div>
              </CardBody>
            </Card>
          )}

          {activeSection === 'integrations' && (
            <Card>
              <CardHeader title="Integrations" subtitle="Connect with external services" />
              <CardBody className="space-y-6">
                <div className="flex items-center justify-between p-4 border border-gray-200 rounded-lg">
                  <div className="flex items-center">
                    <div className="w-10 h-10 bg-gray-900 rounded-lg flex items-center justify-center mr-4">
                      <svg className="w-6 h-6 text-white" fill="currentColor" viewBox="0 0 24 24">
                        <path fillRule="evenodd" d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.531 1.032 1.531 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0112 6.844c.85.004 1.705.115 2.504.337 1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.019 10.019 0 0022 12.017C22 6.484 17.522 2 12 2z" clipRule="evenodd" />
                      </svg>
                    </div>
                    <div>
                      <p className="text-sm font-medium text-gray-900">GitHub</p>
                      <p className="text-xs text-gray-500">Sync with GitHub repositories</p>
                    </div>
                  </div>
                  <button className="btn btn-secondary">Connect</button>
                </div>

                <div className="flex items-center justify-between p-4 border border-gray-200 rounded-lg">
                  <div className="flex items-center">
                    <div className="w-10 h-10 bg-orange-500 rounded-lg flex items-center justify-center mr-4">
                      <svg className="w-6 h-6 text-white" fill="currentColor" viewBox="0 0 24 24">
                        <path d="M22.65 14.39L12 22.13 1.35 14.39a.84.84 0 01-.3-.94l1.22-3.78 2.44-7.51A.42.42 0 014.82 2a.43.43 0 01.58 0 .42.42 0 01.11.18l2.44 7.49h8.1l2.44-7.51A.42.42 0 0118.6 2a.43.43 0 01.58 0 .42.42 0 01.11.18l2.44 7.51L23 13.45a.84.84 0 01-.35.94z" />
                      </svg>
                    </div>
                    <div>
                      <p className="text-sm font-medium text-gray-900">GitLab</p>
                      <p className="text-xs text-gray-500">Sync with GitLab repositories</p>
                    </div>
                  </div>
                  <button className="btn btn-secondary">Connect</button>
                </div>

                <div className="flex items-center justify-between p-4 border border-gray-200 rounded-lg">
                  <div className="flex items-center">
                    <div className="w-10 h-10 bg-blue-600 rounded-lg flex items-center justify-center mr-4">
                      <span className="text-white font-bold text-lg">J</span>
                    </div>
                    <div>
                      <p className="text-sm font-medium text-gray-900">Jira</p>
                      <p className="text-xs text-gray-500">Create issues in Jira</p>
                    </div>
                  </div>
                  <button className="btn btn-secondary">Connect</button>
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Slack Webhook URL
                  </label>
                  <input
                    type="text"
                    value={settings.slackWebhook}
                    onChange={(e) => handleChange('slackWebhook', e.target.value)}
                    placeholder="https://hooks.slack.com/services/..."
                    className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
                  />
                </div>
              </CardBody>
            </Card>
          )}

          {activeSection === 'appearance' && (
            <Card>
              <CardHeader title="Appearance" subtitle="Customize the dashboard look and feel" />
              <CardBody className="space-y-6">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-2">Theme</label>
                  <div className="flex gap-4">
                    {['light', 'dark', 'system'].map((theme) => (
                      <button
                        key={theme}
                        onClick={() => handleChange('theme', theme)}
                        className={`flex-1 p-4 border rounded-lg text-center capitalize transition-colors ${
                          settings.theme === theme
                            ? 'border-primary-500 bg-primary-50 text-primary-700'
                            : 'border-gray-200 hover:border-gray-300'
                        }`}
                      >
                        {theme}
                      </button>
                    ))}
                  </div>
                </div>

                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm font-medium text-gray-700">Compact Mode</p>
                    <p className="text-xs text-gray-500">Reduce spacing for denser display</p>
                  </div>
                  <button
                    onClick={() => handleChange('compactMode', !settings.compactMode)}
                    className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                      settings.compactMode ? 'bg-primary-600' : 'bg-gray-200'
                    }`}
                  >
                    <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                      settings.compactMode ? 'translate-x-6' : 'translate-x-1'
                    }`} />
                  </button>
                </div>

                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm font-medium text-gray-700">Show Line Numbers</p>
                    <p className="text-xs text-gray-500">Display line numbers in code snippets</p>
                  </div>
                  <button
                    onClick={() => handleChange('showLineNumbers', !settings.showLineNumbers)}
                    className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                      settings.showLineNumbers ? 'bg-primary-600' : 'bg-gray-200'
                    }`}
                  >
                    <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                      settings.showLineNumbers ? 'translate-x-6' : 'translate-x-1'
                    }`} />
                  </button>
                </div>
              </CardBody>
            </Card>
          )}

          {/* Save Button */}
          <div className="flex justify-end mt-6">
            <button className="btn btn-primary">Save Settings</button>
          </div>
        </div>
      </div>
    </div>
  );
};

export default Settings;
