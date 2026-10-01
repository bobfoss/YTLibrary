(function () {
  function escapeHtml(value) {
    return String(value || '').replace(/[&<>"']/g, character => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[character]));
  }

  const searchHighlightMark = '<mark class="search-highlight">';

  function searchHighlightTextHtml(value, query) {
    const text = String(value || '');
    const term = String(query || '').trim();
    if (!term) return escapeHtml(text);
    const escapedTerm = term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const matches = text.matchAll(new RegExp(escapedTerm, 'giu'));
    let html = '';
    let offset = 0;
    for (const match of matches) {
      const index = Number(match.index);
      html += escapeHtml(text.slice(offset, index));
      html += `${searchHighlightMark}${escapeHtml(match[0])}</mark>`;
      offset = index + match[0].length;
    }
    return `${html}${escapeHtml(text.slice(offset))}`;
  }

  function searchHighlightSnippetHtml(value) {
    return escapeHtml(value)
      .replaceAll('&lt;mark&gt;', searchHighlightMark)
      .replaceAll('&lt;/mark&gt;', '</mark>');
  }

  function searchHighlightExcerptHtml(value, query, options = {}) {
    const text = String(value || '');
    const term = String(query || '').trim();
    if (!term) return escapeHtml(text);
    const escapedTerm = term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const match = new RegExp(escapedTerm, 'iu').exec(text);
    if (!match) return escapeHtml(text);
    const before = Number.isFinite(Number(options.before))
      ? Math.max(0, Math.floor(Number(options.before)))
      : 64;
    const after = Number.isFinite(Number(options.after))
      ? Math.max(0, Math.floor(Number(options.after)))
      : 140;
    let start = Math.max(0, match.index - before);
    let end = Math.min(text.length, match.index + match[0].length + after);
    if (start > 0) {
      const leadingFragment = /^\S+\s+/.exec(text.slice(start, match.index));
      if (leadingFragment) start += leadingFragment[0].length;
    }
    if (end < text.length) {
      const trailingFragment = /\s+\S*$/.exec(text.slice(match.index + match[0].length, end));
      if (trailingFragment) end -= trailingFragment[0].length;
    }
    const excerpt = `${start > 0 ? '…' : ''}${text.slice(start, end)}${end < text.length ? '…' : ''}`;
    return searchHighlightTextHtml(excerpt, term);
  }

  const searchHighlight = Object.freeze({
    excerptHtml: searchHighlightExcerptHtml,
    snippetHtml: searchHighlightSnippetHtml,
    textHtml: searchHighlightTextHtml,
  });

  function detailRowHtml(items, className = 'details') {
    const filtered = (items || []).filter(Boolean);
    return filtered.length ? `<div class="${className}">${filtered.join('')}</div>` : '';
  }

  function membersOnlyIconHtml() {
    return '<svg class="members-only-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12" width="12" height="12" focusable="false" aria-hidden="true"><path d="M6 .5a5.5 5.5 0 100 11 5.5 5.5 0 000-11Zm.27 2.045.906 1.837 2.027.295a.3.3 0 01.166.511l-1.467 1.43.346 2.019a.3.3 0 01-.435.316L6 8l-1.813.953a.3.3 0 01-.435-.316l.346-2.019-1.467-1.43a.3.3 0 01.166-.511l2.027-.295.907-1.837a.3.3 0 01.539 0Z"></path></svg>';
  }

  function badgeRowsHtml(badges) {
    return detailRowHtml(
      (badges || [])
        .filter(badge => badge && badge.label)
        .map(badge => {
          const rawLabel = String(badge.label || '').trim();
          const membersOnly = ['subscriber_only', 'members only'].includes(rawLabel.toLowerCase());
          const label = membersOnly ? 'Members only' : rawLabel;
          const title = badge.title ? ` title="${escapeHtml(badge.title)}"` : '';
          const className = membersOnly ? 'badge members-only-badge' : 'badge';
          const icon = membersOnly ? membersOnlyIconHtml() : '';
          return `<span class="${className}"${title}>${icon}${escapeHtml(label)}</span>`;
        }),
      'details badge-lines'
    );
  }

  function linkTargetAttributes(url) {
    const value = String(url || '');
    return value.startsWith('/') || value.startsWith('#')
      ? ''
      : ' target="_blank" rel="noreferrer"';
  }

  function creatorHtml(path, name, url) {
    if (!name) return '';
    const avatar = path ? `<img class="channel-avatar" src="/${escapeHtml(path)}" alt="">` : '';
    const content = `${avatar}<span class="creator-name">${escapeHtml(name)}</span>`;
    const attributes = linkTargetAttributes(url);
    return url
      ? `<a class="creator-link creator-chip" href="${escapeHtml(url)}"${attributes}>${content}</a>`
      : `<span class="creator-chip">${content}</span>`;
  }

  function watchProgressPercent(video) {
    const value = Number((video || {}).watch_progress_percent || 0);
    if (!Number.isFinite(value) || value <= 0) return 0;
    return Math.max(1, Math.min(100, Math.round(value)));
  }

  function thumbnailWithProgress(path, video) {
    const wrap = document.createElement('div');
    wrap.className = 'thumb-wrap';
    const image = document.createElement('img');
    image.className = 'thumb';
    image.loading = 'lazy';
    image.alt = '';
    image.src = `/${path}`;
    wrap.append(image);
    const progress = watchProgressPercent(video);
    if (progress) {
      const bar = document.createElement('div');
      bar.className = 'watch-progress';
      bar.style.width = `${progress}%`;
      wrap.append(bar);
    }
    return wrap;
  }

  function watchedLineHtml(video) {
    const progress = watchProgressPercent(video);
    const count = Number((video || {}).watch_count || 0);
    const hasCount = Number.isFinite(count) && count > 0;
    if (!progress && !hasCount) return '';
    const progressHtml = progress
      ? `<span class="watched-progress">Watched ${progress}%</span>`
      : '';
    const countHtml = hasCount
      ? `<span class="watched-count">${progress ? ' · ' : 'Watched · '}${count} ${count === 1 ? 'watch' : 'watches'}</span>`
      : '';
    return `<div class="watched-line${progress ? ' has-progress' : ''}">${progressHtml}${countHtml}</div>`;
  }

  function compactWatchCountHtml(video) {
    const count = Number((video || {}).watch_count || 0);
    if (!Number.isFinite(count) || count <= 0) return '';
    return `<span class="compact-watch-count"> · ${count} ${count === 1 ? 'watch' : 'watches'}</span>`;
  }

  function watchDates(video) {
    return (Array.isArray((video || {}).watch_dates) ? video.watch_dates : [])
      .map(value => String(value || '').slice(0, 10))
      .filter(value => /^\d{4}-\d{2}-\d{2}$/.test(value))
      .sort();
  }

  function watchSparklineHtml(video, options = {}) {
    const dates = watchDates(video);
    if (!dates.length) return '';
    const times = dates.map(date => Date.parse(`${date}T00:00:00Z`)).filter(Number.isFinite);
    if (!times.length) return '';
    const detail = Boolean(options.detail);
    const width = Number(options.width || (detail ? 260 : 86));
    const height = Number(options.height || (detail ? 18 : 14));
    const padding = Number(options.padding || (detail ? 8 : 5));
    const maxBuckets = Number(options.maxBuckets || (detail ? 36 : 18));
    const bucketScale = Number(options.bucketScale || (detail ? 2.2 : 1.4));
    const min = Math.min(...times);
    const max = Math.max(...times);
    const span = Math.max(1, max - min);
    const bucketCount = Math.min(maxBuckets, Math.max(1, Math.ceil(Math.sqrt(times.length) * bucketScale)));
    const buckets = new Map();
    for (const time of times) {
      const index = span === 1 ? 0 : Math.min(bucketCount - 1, Math.floor(((time - min) / span) * bucketCount));
      const bucket = buckets.get(index) || { count: 0, time };
      bucket.count += 1;
      bucket.time = Math.max(bucket.time, time);
      buckets.set(index, bucket);
    }
    const latestIndex = [...buckets.entries()].sort((left, right) => right[1].time - left[1].time)[0]?.[0];
    const maxCount = Math.max(...[...buckets.values()].map(bucket => bucket.count));
    const dots = [...buckets.entries()].sort((left, right) => left[0] - right[0]).map(([index, bucket]) => {
      const x = bucketCount === 1
        ? width - padding
        : padding + (index / (bucketCount - 1)) * (width - padding * 2);
      const radius = Math.min(
        detail ? 5.5 : 4.2,
        (detail ? 2.2 : 1.8) + Math.sqrt(bucket.count / maxCount) * (detail ? 3.3 : 2.4)
      );
      const latest = index === latestIndex ? ' latest' : '';
      return `<circle class="spark-dot${latest}" cx="${x.toFixed(1)}" cy="${(height / 2).toFixed(1)}" r="${radius.toFixed(1)}"></circle>`;
    }).join('');
    const title = `${times.length} ${times.length === 1 ? 'watch' : 'watches'} from ${dates[0]} to ${dates[dates.length - 1]}`;
    return `<svg class="watch-sparkline${detail ? ' detail' : ''}" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(title)}"><title>${escapeHtml(title)}</title>${dots}</svg>`;
  }

  function reactionLabel(video) {
    const reaction = String((video || {}).reaction || '').trim().toUpperCase();
    if (reaction === 'LIKE') return 'Liked';
    if (reaction === 'DISLIKE') return 'Disliked';
    return '';
  }

  function thumbIconHtml(kind, active) {
    const classes = `reaction-icon ${kind}${active ? ' active' : ''}`;
    return `
      <svg class="${classes}" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M7 10v11H3V10h4Zm4.8 11H19c.9 0 1.7-.6 1.9-1.5l1.1-6.2c.2-1.2-.7-2.3-1.9-2.3h-5.4l.8-3.8c.1-.6 0-1.2-.4-1.6L14.2 4 8.7 9.5c-.5.5-.7 1.1-.7 1.8V19c0 1.1.9 2 2 2h1.8Z"></path>
      </svg>
    `;
  }

  function reactionIconsHtml(video) {
    const reaction = String((video || {}).reaction || '').trim().toUpperCase();
    return `
      <div class="reaction-line" title="${escapeHtml(reactionLabel(video) || 'No like/dislike captured')}">
        ${thumbIconHtml('like', reaction === 'LIKE')}
        ${thumbIconHtml('dislike', reaction === 'DISLIKE')}
      </div>
    `;
  }

  function uploaderCategoryHtml(value) {
    const category = String(value || '').trim();
    return category
      ? `<div class="details uploader-category"><span class="uploader-category-label">Uploader category: </span><span>${escapeHtml(category)}</span></div>`
      : '';
  }

  function titleHtml(options) {
    const titleText = String(options.title || '');
    const title = options.titleHtml === undefined
      ? escapeHtml(titleText)
      : String(options.titleHtml || '');
    if (options.titleHref) {
      const target = options.titleTarget ? ` target="${escapeHtml(options.titleTarget)}"` : '';
      const rel = options.titleTarget === '_blank' ? ' rel="noreferrer"' : '';
      return `<div class="title-row"><a class="${escapeHtml(options.titleClass || 'video-title')}" href="${escapeHtml(options.titleHref)}"${target}${rel}>${title}</a><span class="entity-card-slot entity-card-actions" data-entity-card-slot="actions"></span></div>`;
    }
    const localTitle = options.localUrl
      ? `<a class="playlist-title" href="${escapeHtml(options.localUrl)}">${title}</a>`
      : '';
    const externalTitle = options.externalUrl && options.externalIconHtml
      ? `<a class="external-link" href="${escapeHtml(options.externalUrl)}" target="_blank" rel="noreferrer" title="Open on YouTube" aria-label="Open ${escapeHtml(titleText)} on YouTube">${options.externalIconHtml}</a>`
      : '';
    if (localTitle || externalTitle) {
      return `<div class="title-row">${localTitle || `<div class="video-title">${title}</div>`}${externalTitle}<span class="entity-card-slot entity-card-actions" data-entity-card-slot="actions"></span></div>`;
    }
    return `<div class="title-row"><div class="video-title">${title}</div><span class="entity-card-slot entity-card-actions" data-entity-card-slot="actions"></span></div>`;
  }

  function create(options) {
    const article = document.createElement('article');
    article.className = ['card', options.className || ''].filter(Boolean).join(' ');
    if (options.thumbnailPath) {
      article.append(thumbnailWithProgress(options.thumbnailPath, options.progressVideo || {}));
    } else if (options.noThumbnailClass) {
      article.classList.add(options.noThumbnailClass);
    }
    const body = document.createElement('div');
    body.className = 'body';
    body.innerHTML = `
      ${options.resultKind ? `<div class="result-kind">${escapeHtml(options.resultKind)}</div>` : ''}
      ${options.channelHtml ? `<div class="details video-card-channel">${options.channelHtml}</div>` : ''}
      ${options.position ? `<div class="position">#${escapeHtml(options.position)}</div>` : ''}
      ${titleHtml(options)}
      ${options.availabilityHtml
        ? `<div class="video-availability-row">${options.availabilityHtml}${options.typeDecoratorHtml || ''}${options.aiDisclosureDecoratorHtml || ''}${options.autoDubbedDecoratorHtml || ''}${options.compactAvailabilityHtml || ''}<span class="entity-card-slot entity-card-primary-metadata" data-entity-card-slot="primaryMetadata"></span></div>`
        : ''}
      ${options.movieMetadataHtml || ''}
      ${options.featureMetadataHtml || ''}
      ${options.contentWarningHtml || ''}
      ${badgeRowsHtml(options.badges)}
      ${detailRowHtml(options.details)}
      ${detailRowHtml(options.durationDetails, 'details video-duration-details')}
      ${detailRowHtml(options.identifiers, 'details video-identifiers')}
      ${options.recoveryHtml || ''}
      ${options.watchDateHtml || ''}
      ${options.latestWatchDateHtml || ''}
      ${options.watchedHtml || ''}
      ${options.sparklineHtml || ''}
      ${options.reactionHtml || ''}
      ${uploaderCategoryHtml(options.uploaderCategory)}
      <div class="entity-card-decoration-row">
        <div class="entity-card-slot entity-card-secondary-metadata" data-entity-card-slot="secondaryMetadata"></div>
        ${options.annotationMetaHtml || ''}
      </div>
      ${options.annotationNoteHtml || ''}
      <div class="search-result-summary-slot" data-search-result-slot="summaries"></div>
      ${options.descriptionHtml
        ? `<div class="description" data-search-result-native-summary>${options.descriptionHtml}</div>`
        : (options.description ? `<div class="description" data-search-result-native-summary>${escapeHtml(options.description)}</div>` : '')}
      ${detailRowHtml(options.sources)}
      ${options.playlistSourcesHtml || ''}
    `;
    article.append(body);
    return article;
  }

  window.YTLibraryVideoCard = {
    badgeRowsHtml,
    compactWatchCountHtml,
    create,
    creatorHtml,
    detailRowHtml,
    escapeHtml,
    linkTargetAttributes,
    membersOnlyIconHtml,
    reactionLabel,
    reactionIconsHtml,
    searchHighlight,
    thumbnailWithProgress,
    thumbIconHtml,
    uploaderCategoryHtml,
    watchProgressPercent,
    watchSparklineHtml,
    watchedLineHtml,
  };
})();
