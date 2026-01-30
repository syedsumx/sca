import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../../contexts/AuthContext';
import api from '../../services/api';

interface SSOProvider {
  name: string;
  authorize_url: string;
}

const Login: React.FC = () => {
  const [isRegister, setIsRegister] = useState(false);
  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [ssoProviders, setSsoProviders] = useState<SSOProvider[]>([]);

  const { login, register, isAuthenticated } = useAuth();
  const navigate = useNavigate();

  useEffect(() => {
    if (isAuthenticated) {
      navigate('/');
    }
  }, [isAuthenticated, navigate]);

  useEffect(() => {
    // Load SSO providers
    api.getSSOProviders()
      .then(setSsoProviders)
      .catch(() => {}); // SSO not configured — that's fine
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      if (isRegister) {
        await register(username, email, password, displayName);
      } else {
        await login(username, password);
      }
      navigate('/');
    } catch (err: any) {
      setError(
        err?.response?.data?.detail ||
        (isRegister ? 'Registration failed' : 'Invalid username or password')
      );
    } finally {
      setLoading(false);
    }
  };

  const handleSSO = (authorizeUrl: string) => {
    const width = 600;
    const height = 700;
    const left = window.screenX + (window.outerWidth - width) / 2;
    const top = window.screenY + (window.outerHeight - height) / 2;
    window.open(
      authorizeUrl,
      'codescope_sso',
      `width=${width},height=${height},left=${left},top=${top}`,
    );
  };

  const ssoLabels: Record<string, { label: string; bg: string }> = {
    github: { label: 'Continue with GitHub', bg: 'bg-gray-900 hover:bg-gray-800' },
    gitlab: { label: 'Continue with GitLab', bg: 'bg-orange-600 hover:bg-orange-500' },
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 py-12 px-4">
      <div className="max-w-md w-full space-y-8">
        {/* Logo */}
        <div className="text-center">
          <div className="mx-auto w-12 h-12 bg-primary-600 rounded-xl flex items-center justify-center">
            <span className="text-white font-bold text-2xl">C</span>
          </div>
          <h2 className="mt-4 text-3xl font-bold text-gray-900">CodeScope</h2>
          <p className="mt-2 text-sm text-gray-500">
            {isRegister ? 'Create your account' : 'Sign in to your account'}
          </p>
        </div>

        {/* SSO Buttons */}
        {ssoProviders.length > 0 && (
          <div className="space-y-3">
            {ssoProviders.map((provider) => {
              const info = ssoLabels[provider.name] || {
                label: `Continue with ${provider.name}`,
                bg: 'bg-gray-600 hover:bg-gray-500',
              };
              return (
                <button
                  key={provider.name}
                  onClick={() => handleSSO(provider.authorize_url)}
                  className={`w-full flex items-center justify-center gap-3 px-4 py-3 text-white font-medium rounded-lg transition-colors ${info.bg}`}
                >
                  {info.label}
                </button>
              );
            })}
            <div className="relative">
              <div className="absolute inset-0 flex items-center">
                <div className="w-full border-t border-gray-300" />
              </div>
              <div className="relative flex justify-center text-sm">
                <span className="px-2 bg-gray-50 text-gray-500">or</span>
              </div>
            </div>
          </div>
        )}

        {/* Login / Register Form */}
        <form onSubmit={handleSubmit} className="space-y-6">
          {error && (
            <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg text-sm">
              {error}
            </div>
          )}

          <div className="space-y-4">
            <div>
              <label htmlFor="username" className="block text-sm font-medium text-gray-700">
                Username
              </label>
              <input
                id="username"
                type="text"
                required
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                className="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-lg shadow-sm focus:ring-primary-500 focus:border-primary-500"
                placeholder="Enter your username"
              />
            </div>

            {isRegister && (
              <>
                <div>
                  <label htmlFor="email" className="block text-sm font-medium text-gray-700">
                    Email
                  </label>
                  <input
                    id="email"
                    type="email"
                    required
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    className="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-lg shadow-sm focus:ring-primary-500 focus:border-primary-500"
                    placeholder="you@example.com"
                  />
                </div>
                <div>
                  <label htmlFor="displayName" className="block text-sm font-medium text-gray-700">
                    Display Name (optional)
                  </label>
                  <input
                    id="displayName"
                    type="text"
                    value={displayName}
                    onChange={(e) => setDisplayName(e.target.value)}
                    className="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-lg shadow-sm focus:ring-primary-500 focus:border-primary-500"
                  />
                </div>
              </>
            )}

            <div>
              <label htmlFor="password" className="block text-sm font-medium text-gray-700">
                Password
              </label>
              <input
                id="password"
                type="password"
                required
                minLength={isRegister ? 8 : undefined}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-lg shadow-sm focus:ring-primary-500 focus:border-primary-500"
                placeholder={isRegister ? 'Minimum 8 characters' : 'Enter your password'}
              />
            </div>
          </div>

          <button
            type="submit"
            disabled={loading}
            className="w-full flex justify-center py-3 px-4 border border-transparent rounded-lg shadow-sm text-sm font-medium text-white bg-primary-600 hover:bg-primary-700 focus:ring-2 focus:ring-offset-2 focus:ring-primary-500 disabled:opacity-50 transition-colors"
          >
            {loading ? 'Please wait...' : isRegister ? 'Create Account' : 'Sign In'}
          </button>

          <div className="text-center">
            <button
              type="button"
              onClick={() => { setIsRegister(!isRegister); setError(''); }}
              className="text-sm text-primary-600 hover:text-primary-500"
            >
              {isRegister ? 'Already have an account? Sign in' : "Don't have an account? Register"}
            </button>
          </div>
        </form>

        {/* Default credentials hint */}
        {!isRegister && (
          <div className="text-center text-xs text-gray-400">
            Default admin: admin / admin
          </div>
        )}
      </div>
    </div>
  );
};

export default Login;
