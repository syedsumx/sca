import React, { useState } from 'react';
import { DocumentArrowDownIcon, ListBulletIcon } from '@heroicons/react/24/outline';

const mockComponents = [
  { name: 'fastapi', version: '0.104.1', purl: 'pkg:pypi/fastapi@0.104.1', license: 'MIT', scope: 'required' },
  { name: 'uvicorn', version: '0.24.0', purl: 'pkg:pypi/uvicorn@0.24.0', license: 'BSD-3-Clause', scope: 'required' },
  { name: 'typer', version: '0.9.0', purl: 'pkg:pypi/typer@0.9.0', license: 'MIT', scope: 'required' },
  { name: 'rich', version: '13.7.0', purl: 'pkg:pypi/rich@13.7.0', license: 'MIT', scope: 'required' },
  { name: 'reportlab', version: '4.0.8', purl: 'pkg:pypi/reportlab@4.0.8', license: 'BSD-3-Clause', scope: 'required' },
  { name: 'pytest', version: '7.4.4', purl: 'pkg:pypi/pytest@7.4.4', license: 'MIT', scope: 'dev' },
  { name: 'react', version: '18.2.0', purl: 'pkg:npm/react@18.2.0', license: 'MIT', scope: 'required' },
  { name: 'tailwindcss', version: '3.4.0', purl: 'pkg:npm/tailwindcss@3.4.0', license: 'MIT', scope: 'dev' },
];

const SBOM: React.FC = () => {
  const [format, setFormat] = useState<'cyclonedx' | 'spdx'>('cyclonedx');
  const [search, setSearch] = useState('');

  const filtered = mockComponents.filter(c =>
    c.name.toLowerCase().includes(search.toLowerCase()) ||
    c.license.toLowerCase().includes(search.toLowerCase())
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-gray-900">Software Bill of Materials</h1>
        <div className="flex items-center gap-3">
          <select
            value={format}
            onChange={e => setFormat(e.target.value as any)}
            className="border border-gray-300 rounded-lg px-3 py-2 text-sm"
          >
            <option value="cyclonedx">CycloneDX 1.5</option>
            <option value="spdx">SPDX 2.3</option>
          </select>
          <button className="flex items-center gap-2 px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700">
            <DocumentArrowDownIcon className="w-4 h-4" />
            Export {format.toUpperCase()}
          </button>
        </div>
      </div>

      {/* Summary */}
      <div className="grid grid-cols-3 gap-4">
        <div className="bg-white rounded-lg border p-4">
          <ListBulletIcon className="w-6 h-6 text-primary-600" />
          <p className="text-2xl font-bold mt-1">{mockComponents.length}</p>
          <p className="text-sm text-gray-500">Total Components</p>
        </div>
        <div className="bg-white rounded-lg border p-4">
          <p className="text-2xl font-bold">{mockComponents.filter(c => c.scope === 'required').length}</p>
          <p className="text-sm text-gray-500">Production</p>
        </div>
        <div className="bg-white rounded-lg border p-4">
          <p className="text-2xl font-bold">{mockComponents.filter(c => c.scope === 'dev').length}</p>
          <p className="text-sm text-gray-500">Development</p>
        </div>
      </div>

      {/* Search */}
      <input
        type="text"
        placeholder="Search components..."
        value={search}
        onChange={e => setSearch(e.target.value)}
        className="w-full px-4 py-2 border border-gray-300 rounded-lg"
      />

      {/* Components Table */}
      <div className="bg-white rounded-lg border overflow-hidden">
        <table className="min-w-full divide-y divide-gray-200">
          <thead className="bg-gray-50">
            <tr>
              {['Name', 'Version', 'Package URL', 'License', 'Scope'].map(h => (
                <th key={h} className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-200">
            {filtered.map((c, i) => (
              <tr key={i} className="hover:bg-gray-50">
                <td className="px-4 py-3 text-sm font-medium">{c.name}</td>
                <td className="px-4 py-3 text-sm">{c.version}</td>
                <td className="px-4 py-3 text-sm font-mono text-gray-500 text-xs">{c.purl}</td>
                <td className="px-4 py-3 text-sm"><span className="px-2 py-1 bg-blue-50 text-blue-700 rounded text-xs">{c.license}</span></td>
                <td className="px-4 py-3 text-sm"><span className={`px-2 py-1 rounded text-xs ${c.scope === 'dev' ? 'bg-gray-100 text-gray-600' : 'bg-green-50 text-green-700'}`}>{c.scope}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};

export default SBOM;
