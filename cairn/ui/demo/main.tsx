import React from 'react';
import ReactDOM from 'react-dom';

import { ConsumerApp } from '../src/consumer/ConsumerApp';
import { ConsumerClient } from '../src/consumer/api/client';
import { demoFetch } from './demoApi';

const client = new ConsumerClient({ baseUrl: '/cairn/api', graphqlUrl: '/api/v2/graphql', fetchImpl: demoFetch as typeof fetch });

if (!window.location.hash) window.location.hash = '#/discover';
ReactDOM.render(<ConsumerApp routerMode="hash" client={client} catalogUrl={null} />, document.getElementById('cairn-root'));
