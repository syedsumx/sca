// Active Metadata consumer layer (UX_ACTIVE_METADATA_SPEC.md).
export * from './api/types';
export { ConsumerClient, GraphQLError, SEARCH_QUERY, filtersToInput, parseSearch } from './api/client';
export { QueryCache, defaultCache, optimistic } from './api/cache';
export { ConsumerApiProvider, useConsumerApi, useConsumerClient, useDheQuery, useOptimistic } from './api/hooks';
export { DheProvider, useDheSession } from './context/DheProvider';
export { ConsumerApp, ConsumerRoutes, CONSUMER_ROUTES } from './ConsumerApp';
export { mount } from './mount';
export { TrustBadges, ConfidenceMeter } from './components/TrustBadges';
export { AssetCard, AssetRow } from './components/Assets';
export { BookmarkToggle, FollowButton } from './components/Engagement';
export { OmniSearchBar } from './components/OmniSearchBar';
export { FacetPanel } from './components/FacetPanel';
export { DiscussionPanel, ThreadView, CommentComposer } from './components/Discussion';
export { ProductCard, RequestAccessDrawer } from './components/Marketplace';
export { AssistantChat, AssistantSlideOver } from './components/Assistant';
export { MarkdownView, renderMarkdown } from './utils/markdown';
export { parseSearchState, serializeSearchState, toggleFilter } from './utils/urlState';
