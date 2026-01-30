import React, { createContext, useContext, useState, useEffect, useCallback } from 'react';
import api from '../services/api';

interface AuthUser {
  user_id: string;
  username: string;
  email: string;
  display_name: string;
  role: string;
  is_sso: boolean;
  sso_provider: string;
}

interface AuthState {
  user: AuthUser | null;
  isAuthenticated: boolean;
  isLoading: boolean;
}

interface AuthContextType extends AuthState {
  login: (username: string, password: string) => Promise<void>;
  register: (username: string, email: string, password: string, displayName?: string) => Promise<void>;
  logout: () => Promise<void>;
  refreshAuth: () => Promise<void>;
  handleSSOCallback: (data: { access_token: string; refresh_token: string; user: AuthUser }) => void;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

const TOKEN_KEY = 'codescope_token';
const REFRESH_KEY = 'codescope_refresh';
const USER_KEY = 'codescope_user';

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [state, setState] = useState<AuthState>({
    user: null,
    isAuthenticated: false,
    isLoading: true,
  });

  // Load saved auth state on mount
  useEffect(() => {
    const token = localStorage.getItem(TOKEN_KEY);
    const userJson = localStorage.getItem(USER_KEY);

    if (token && userJson) {
      try {
        const user = JSON.parse(userJson) as AuthUser;
        setState({ user, isAuthenticated: true, isLoading: false });
      } catch {
        clearAuth();
        setState({ user: null, isAuthenticated: false, isLoading: false });
      }
    } else {
      setState({ user: null, isAuthenticated: false, isLoading: false });
    }
  }, []);

  // Listen for SSO callback messages
  useEffect(() => {
    const handleMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      if (event.data?.type === 'sso_callback') {
        handleSSOCallback(event.data);
      }
    };
    window.addEventListener('message', handleMessage);
    return () => window.removeEventListener('message', handleMessage);
  }, []);

  const saveAuth = (accessToken: string, refreshToken: string, user: AuthUser) => {
    localStorage.setItem(TOKEN_KEY, accessToken);
    localStorage.setItem(REFRESH_KEY, refreshToken);
    localStorage.setItem(USER_KEY, JSON.stringify(user));
    setState({ user, isAuthenticated: true, isLoading: false });
  };

  const clearAuth = () => {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(REFRESH_KEY);
    localStorage.removeItem(USER_KEY);
  };

  const login = async (username: string, password: string) => {
    const response = await api.login(username, password);
    saveAuth(response.access_token, response.refresh_token, response.user);
  };

  const register = async (username: string, email: string, password: string, displayName?: string) => {
    const response = await api.register(username, email, password, displayName);
    saveAuth(response.access_token, response.refresh_token, response.user);
  };

  const logout = async () => {
    try {
      await api.logout();
    } catch {
      // Ignore errors — we clear local state regardless
    }
    clearAuth();
    setState({ user: null, isAuthenticated: false, isLoading: false });
  };

  const refreshAuth = useCallback(async () => {
    const refreshToken = localStorage.getItem(REFRESH_KEY);
    if (!refreshToken) {
      clearAuth();
      setState({ user: null, isAuthenticated: false, isLoading: false });
      return;
    }
    try {
      const response = await api.refreshToken(refreshToken);
      saveAuth(response.access_token, response.refresh_token, response.user);
    } catch {
      clearAuth();
      setState({ user: null, isAuthenticated: false, isLoading: false });
    }
  }, []);

  const handleSSOCallback = (data: { access_token: string; refresh_token: string; user: AuthUser }) => {
    saveAuth(data.access_token, data.refresh_token, data.user);
  };

  return (
    <AuthContext.Provider value={{
      ...state,
      login,
      register,
      logout,
      refreshAuth,
      handleSSOCallback,
    }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = (): AuthContextType => {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};

export default AuthContext;
