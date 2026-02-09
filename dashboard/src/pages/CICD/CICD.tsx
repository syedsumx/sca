import React, { useState } from 'react';
import { ClipboardDocumentIcon, CheckIcon } from '@heroicons/react/24/outline';

const platforms = [
  { id: 'github', name: 'GitHub Actions', icon: '🔧' },
  { id: 'gitlab', name: 'GitLab CI', icon: '🦊' },
  { id: 'jenkins', name: 'Jenkins', icon: '🏗️' },
  { id: 'azure', name: 'Azure Pipelines', icon: '☁️' },
];

const CICD: React.FC = () => {
  const [platform, setPlatform] = useState('github');
  const [projectName, setProjectName] = useState('my-project');
  const [qualityGate, setQualityGate] = useState(true);
  const [failOnVulns, setFailOnVulns] = useState(true);
  const [aiVetting, setAiVetting] = useState(false);
  const [coverage, setCoverage] = useState(80);
  const [copied, setCopied] = useState(false);
  const [generated, setGenerated] = useState('');

  const handleGenerate = () => {
    // In production this calls the API; mock for now
    const filename: Record<string, string> = {
      github: '.github/workflows/codescope.yml',
      gitlab: '.gitlab-ci.yml',
      jenkins: 'Jenkinsfile',
      azure: 'azure-pipelines.yml',
    };
    setGenerated(`# CodeScope CI/CD for ${projectName}\n# Platform: ${platforms.find(p => p.id === platform)?.name}\n# Save as: ${filename[platform]}\n#\n# Quality Gate: ${qualityGate ? 'enabled' : 'disabled'}\n# Fail on vulnerabilities: ${failOnVulns}\n# AI Vetting: ${aiVetting}\n# Coverage threshold: ${coverage}%\n#\n# Generate the full config via the API:\n# POST /api/v1/cicd/generate/${platform}`);
  };

  const handleCopy = () => {
    navigator.clipboard.writeText(generated);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-gray-900">CI/CD Integration</h1>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Config Panel */}
        <div className="bg-white rounded-lg border p-6 space-y-4">
          <h2 className="text-lg font-semibold">Configuration</h2>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">Platform</label>
            <div className="grid grid-cols-2 gap-2">
              {platforms.map(p => (
                <button
                  key={p.id}
                  onClick={() => setPlatform(p.id)}
                  className={`flex items-center gap-2 p-3 rounded-lg border text-sm font-medium transition-colors
                    ${platform === p.id ? 'border-primary-500 bg-primary-50 text-primary-700' : 'border-gray-200 hover:bg-gray-50'}`}
                >
                  <span>{p.icon}</span> {p.name}
                </button>
              ))}
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700">Project Name</label>
            <input type="text" value={projectName} onChange={e => setProjectName(e.target.value)}
              className="mt-1 w-full px-3 py-2 border border-gray-300 rounded-lg" />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700">Coverage Threshold</label>
            <input type="number" value={coverage} onChange={e => setCoverage(+e.target.value)}
              className="mt-1 w-24 px-3 py-2 border border-gray-300 rounded-lg" min={0} max={100} />
            <span className="ml-2 text-sm text-gray-500">%</span>
          </div>

          <div className="space-y-2">
            {[
              { label: 'Enable Quality Gate', checked: qualityGate, onChange: setQualityGate },
              { label: 'Fail on Vulnerabilities', checked: failOnVulns, onChange: setFailOnVulns },
              { label: 'AI Code Vetting', checked: aiVetting, onChange: setAiVetting },
            ].map(({ label, checked, onChange }) => (
              <label key={label} className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={checked} onChange={e => onChange(e.target.checked)}
                  className="rounded border-gray-300 text-primary-600 focus:ring-primary-500" />
                {label}
              </label>
            ))}
          </div>

          <button onClick={handleGenerate}
            className="w-full px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 font-medium">
            Generate Pipeline Config
          </button>
        </div>

        {/* Output Panel */}
        <div className="bg-white rounded-lg border p-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold">Generated Config</h2>
            {generated && (
              <button onClick={handleCopy}
                className="flex items-center gap-1 px-3 py-1.5 text-sm border rounded-lg hover:bg-gray-50">
                {copied ? <CheckIcon className="w-4 h-4 text-green-600" /> : <ClipboardDocumentIcon className="w-4 h-4" />}
                {copied ? 'Copied!' : 'Copy'}
              </button>
            )}
          </div>
          {generated ? (
            <pre className="bg-gray-900 text-green-400 p-4 rounded-lg overflow-auto text-sm font-mono max-h-96">
              {generated}
            </pre>
          ) : (
            <div className="flex items-center justify-center h-48 text-gray-400">
              Configure and click "Generate Pipeline Config"
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default CICD;
