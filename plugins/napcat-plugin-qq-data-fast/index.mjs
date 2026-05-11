const KCHATTYPE_C2C = 1;
const KCHATTYPE_GROUP = 2;
const KCHATTYPE_TEMP_C2C_FROM_GROUP = 100;
const DEFAULT_PAGE_SIZE = 200;
const MAX_PAGE_SIZE = 200;

function asText(value) {
  if (value === null || value === undefined) {
    return "";
  }
  return String(value).trim();
}

function asBool(value, fallback = false) {
  if (typeof value === "boolean") {
    return value;
  }
  if (typeof value === "string") {
    if (value.toLowerCase() === "true") {
      return true;
    }
    if (value.toLowerCase() === "false") {
      return false;
    }
  }
  return fallback;
}

function clampCount(value) {
  const parsed = Number(value || DEFAULT_PAGE_SIZE);
  if (!Number.isFinite(parsed)) {
    return DEFAULT_PAGE_SIZE;
  }
  return Math.max(1, Math.min(MAX_PAGE_SIZE, Math.trunc(parsed)));
}

function clampPositiveCount(value, fallback = DEFAULT_PAGE_SIZE) {
  const parsed = Number(value || fallback);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return Math.max(1, Math.trunc(parsed));
}

function pick(source, keys) {
  if (!source || typeof source !== "object") {
    return undefined;
  }
  const output = {};
  for (const key of keys) {
    if (source[key] !== undefined) {
      output[key] = source[key];
    }
  }
  return Object.keys(output).length > 0 ? output : undefined;
}

function slimElement(element) {
  const base = {
    elementType: element?.elementType,
    elementId: element?.elementId ?? "",
  };
  const elementType = Number(element?.elementType || 0);

  if (elementType === 1 && element?.textElement) {
    return {
      ...base,
      textElement: pick(element.textElement, [
        "content",
        "atType",
        "atUid",
        "atTinyId",
        "atNtUid",
      ]),
    };
  }

  if (elementType === 2 && element?.picElement) {
    return {
      ...base,
      picElement: pick(element.picElement, [
        "fileName",
        "fileUuid",
        "sourcePath",
        "md5HexStr",
        "summary",
        "picWidth",
        "picHeight",
        "originImageUrl",
        "filePath",
        "fileSize",
      ]),
    };
  }

  if (elementType === 3 && element?.fileElement) {
    return {
      ...base,
      fileElement: pick(element.fileElement, [
        "fileName",
        "filePath",
        "fileMd5",
        "fileSize",
        "fileUuid",
        "fileBizId",
      ]),
    };
  }

  if (elementType === 4 && element?.pttElement) {
    return {
      ...base,
      pttElement: pick(element.pttElement, [
        "fileName",
        "filePath",
        "md5HexStr",
        "fileSize",
        "fileUuid",
      ]),
    };
  }

  if (elementType === 6 && element?.faceElement) {
    return {
      ...base,
      faceElement: pick(element.faceElement, [
        "faceIndex",
        "faceType",
        "faceText",
        "resultId",
        "chainCount",
        "stickerId",
        "stickerType",
        "packId",
      ]),
    };
  }

  if (elementType === 7 && element?.replyElement) {
    return {
      ...base,
      replyElement: pick(element.replyElement, [
        "replayMsgSeq",
        "replayMsgId",
        "replyMsgTime",
        "senderUid",
        "senderUin",
        "senderUidStr",
        "replyMsgClientSeq",
      ]),
    };
  }

  if (elementType === 11 && element?.marketFaceElement) {
    return {
      ...base,
      marketFaceElement: pick(element.marketFaceElement, [
        "faceName",
        "emojiId",
        "emojiPackageId",
        "key",
        "staticFacePath",
        "dynamicFacePath",
      ]),
    };
  }

  if (element?.videoElement) {
    return {
      ...base,
      videoElement: pick(element.videoElement, [
        "fileName",
        "filePath",
        "fileUuid",
        "fileSize",
        "md5HexStr",
      ]),
    };
  }

  for (const key of Object.keys(element || {})) {
    if (key.endsWith("Element") && element[key] !== undefined && element[key] !== null) {
      return {
        ...base,
        [key]: element[key],
      };
    }
  }

  return base;
}

function slimRawMessage(message) {
  const senderName =
    asText(message?.sendRemarkName) ||
    asText(message?.sendMemberName) ||
    asText(message?.sendNickName) ||
    asText(message?.senderUin);

  return {
    time: Number(message?.msgTime || 0),
    user_id: asText(message?.senderUin),
    message_id: asText(message?.msgId),
    message_seq: asText(message?.msgSeq),
    anchor_message_id: asText(message?.msgId),
    sender: {
      uin: asText(message?.senderUin),
      uid: asText(message?.senderUid),
      name: senderName,
      nickname: asText(message?.sendNickName),
      card: asText(message?.sendMemberName),
      remark: asText(message?.sendRemarkName),
    },
    isRecalled: asText(message?.recallTime) !== "" && asText(message?.recallTime) !== "0",
    rawMessage: {
      guildId: asText(message?.guildId),
      msgRandom: asText(message?.msgRandom),
      msgId: asText(message?.msgId),
      msgTime: asText(message?.msgTime),
      msgSeq: asText(message?.msgSeq),
      msgType: message?.msgType,
      subMsgType: message?.subMsgType,
      senderUid: asText(message?.senderUid),
      senderUin: asText(message?.senderUin),
      peerUid: asText(message?.peerUid),
      peerUin: asText(message?.peerUin),
      remark: asText(message?.remark),
      peerName: asText(message?.peerName),
      sendNickName: asText(message?.sendNickName),
      sendRemarkName: asText(message?.sendRemarkName),
      sendMemberName: asText(message?.sendMemberName),
      chatType: message?.chatType,
      recallTime: asText(message?.recallTime),
      sourceType: message?.sourceType,
      isOnlineMsg: Boolean(message?.isOnlineMsg),
      clientSeq: asText(message?.clientSeq),
      parentMsgIdList: Array.isArray(message?.parentMsgIdList) ? message.parentMsgIdList : [],
      elements: Array.isArray(message?.elements) ? message.elements.map(slimElement) : [],
    },
  };
}

async function buildPeer(ctx, chatType, chatId) {
  if (chatType === "group") {
    return {
      chatType: KCHATTYPE_GROUP,
      peerUid: asText(chatId),
      guildId: "",
    };
  }

  const uid = await ctx.core.apis.UserApi.getUidByUinV2(asText(chatId));
  if (!uid) {
    throw new Error(`Friend ${chatId} does not exist`);
  }
  const isBuddy = await ctx.core.apis.FriendApi.isBuddy(uid);
  return {
    chatType: isBuddy ? KCHATTYPE_C2C : KCHATTYPE_TEMP_C2C_FROM_GROUP,
    peerUid: uid,
    guildId: "",
  };
}

async function fetchHistory(ctx, payload) {
  const chatType = payload?.chat_type === "group" ? "group" : "private";
  const chatId = asText(payload?.chat_id || payload?.group_id || payload?.user_id);
  if (!chatId) {
    throw new Error("chat_id is required");
  }
  const peer = await buildPeer(ctx, chatType, chatId);
  const count = clampCount(payload?.count);
  const reverseOrder = asBool(payload?.reverse_order, false);
  const messageId = asText(payload?.message_id || payload?.anchor_message_id);
  const response = messageId
    ? await ctx.core.apis.MsgApi.getMsgHistory(peer, messageId, count, reverseOrder)
    : await ctx.core.apis.MsgApi.getAioFirstViewLatestMsgs(peer, count);
  const msgList = Array.isArray(response?.msgList) ? response.msgList : [];
  return {
    chat_type: chatType,
    chat_id: chatId,
    count: msgList.length,
    messages: msgList.map(slimRawMessage),
  };
}

function rawHistoryAnchor(message) {
  return asText(message?.msgId || message?.msgSeq);
}

function rawHistoryKey(message) {
  return [
    asText(message?.msgSeq),
    asText(message?.msgId),
    asText(message?.msgTime),
    asText(message?.senderUin),
  ].join("|");
}

function compareHistoryOrdinals(left, right) {
  const leftText = asText(left);
  const rightText = asText(right);
  if (/^\d+$/.test(leftText) && /^\d+$/.test(rightText)) {
    if (leftText.length !== rightText.length) {
      return leftText.length - rightText.length;
    }
    if (leftText < rightText) {
      return -1;
    }
    if (leftText > rightText) {
      return 1;
    }
    return 0;
  }
  return leftText.localeCompare(rightText);
}

function sortHistoryMessages(messages) {
  return [...messages].sort((left, right) => {
    const leftTime = Number(left?.msgTime || 0);
    const rightTime = Number(right?.msgTime || 0);
    if (leftTime !== rightTime) {
      return leftTime - rightTime;
    }
    const seqCompare = compareHistoryOrdinals(left?.msgSeq, right?.msgSeq);
    if (seqCompare !== 0) {
      return seqCompare;
    }
    return compareHistoryOrdinals(left?.msgId, right?.msgId);
  });
}

const HISTORY_FETCH_STRATEGY_MSG_HISTORY = "msg_history";
const HISTORY_FETCH_STRATEGY_SEQ_WINDOW = "seq_window";
const HISTORY_FETCH_STRATEGY_QUERY_SEQ_WINDOW = "query_seq_window";

function normalizeHistoryFetchStrategy(value) {
  const text = asText(value).toLowerCase();
  if (text === HISTORY_FETCH_STRATEGY_SEQ_WINDOW) {
    return HISTORY_FETCH_STRATEGY_SEQ_WINDOW;
  }
  if (text === HISTORY_FETCH_STRATEGY_QUERY_SEQ_WINDOW) {
    return HISTORY_FETCH_STRATEGY_QUERY_SEQ_WINDOW;
  }
  return HISTORY_FETCH_STRATEGY_MSG_HISTORY;
}

function rawHistorySeq(message) {
  return asText(message?.msgSeq);
}

async function queryLatestHistoryWindow(ctx, peer, pageSize) {
  return await ctx.core.context.session.getMsgService().queryMsgsWithFilterEx("0", "0", "0", {
    chatInfo: peer,
    filterMsgType: [],
    filterSendersUid: [],
    filterMsgToTime: "0",
    filterMsgFromTime: "0",
    isReverseOrder: true,
    isIncludeCurrent: true,
    pageLimit: pageSize,
  });
}

async function fetchHistoryWindow(ctx, peer, pageSize, strategy, anchorMessageId, anchorMessageSeq) {
  const startedAt = Date.now();
  let response;
  let route = HISTORY_FETCH_STRATEGY_MSG_HISTORY;
  if (strategy === HISTORY_FETCH_STRATEGY_QUERY_SEQ_WINDOW) {
    if (anchorMessageSeq) {
      route = "getMsgsBySeqAndCount";
      response = await ctx.core.apis.MsgApi.getMsgsBySeqAndCount(peer, anchorMessageSeq, pageSize, true, true);
    } else if (anchorMessageId) {
      route = "getMsgHistory";
      response = await ctx.core.apis.MsgApi.getMsgHistory(peer, anchorMessageId, pageSize, true);
    } else {
      route = "queryMsgsWithFilterEx";
      response = await queryLatestHistoryWindow(ctx, peer, pageSize);
    }
  } else if (strategy === HISTORY_FETCH_STRATEGY_SEQ_WINDOW) {
    if (anchorMessageSeq) {
      route = "getMsgsBySeqAndCount";
      response = await ctx.core.apis.MsgApi.getMsgsBySeqAndCount(peer, anchorMessageSeq, pageSize, true, true);
    } else if (anchorMessageId) {
      route = "getMsgHistory";
      response = await ctx.core.apis.MsgApi.getMsgHistory(peer, anchorMessageId, pageSize, true);
    } else {
      route = "getAioFirstViewLatestMsgs";
      response = await ctx.core.apis.MsgApi.getAioFirstViewLatestMsgs(peer, pageSize);
    }
  } else if (anchorMessageId) {
    route = "getMsgHistory";
    response = await ctx.core.apis.MsgApi.getMsgHistory(peer, anchorMessageId, pageSize, true);
  } else {
    route = "getAioFirstViewLatestMsgs";
    response = await ctx.core.apis.MsgApi.getAioFirstViewLatestMsgs(peer, pageSize);
  }
  return {
    response,
    route,
    elapsedMs: Math.max(0, Date.now() - startedAt),
  };
}

async function fetchHistoryTailBulk(ctx, payload) {
  const chatType = payload?.chat_type === "group" ? "group" : "private";
  const chatId = asText(payload?.chat_id || payload?.group_id || payload?.user_id);
  if (!chatId) {
    throw new Error("chat_id is required");
  }
  const requestedDataCount = clampPositiveCount(
    payload?.data_count || payload?.requested_data_count || payload?.count,
    DEFAULT_PAGE_SIZE,
  );
  const pageSize = clampCount(payload?.page_size || payload?.count || DEFAULT_PAGE_SIZE);
  const startAnchorMessageId = asText(
    payload?.anchor_message_id || payload?.anchorMessageId || payload?.message_id,
  );
  const startAnchorMessageSeq = asText(
    payload?.anchor_message_seq || payload?.anchorMessageSeq || payload?.message_seq,
  );
  const historyFetchStrategy = normalizeHistoryFetchStrategy(
    payload?.history_fetch_strategy || payload?.strategy,
  );
  const includeDebugStats = asBool(payload?.include_debug_stats, false);
  const peer = await buildPeer(ctx, chatType, chatId);
  const seenKeys = new Set();
  const seenAnchors = new Set();
  const pageSegments = [];
  let anchor = startAnchorMessageId;
  let anchorSeq = startAnchorMessageSeq;
  let pagesScanned = 0;
  let noProgressStreak = 0;
  let exhausted = false;
  const chunkStats = [];
  let collectedCount = 0;

  while (collectedCount < requestedDataCount) {
    const pageCall = await fetchHistoryWindow(
      ctx,
      peer,
      pageSize,
      historyFetchStrategy,
      anchor,
      anchorSeq,
    );
    const response = pageCall.response;
    const rawPage = Array.isArray(response?.msgList) ? response.msgList : [];
    if (includeDebugStats) {
      chunkStats.push({
        chunk_index: chunkStats.length + 1,
        route: pageCall.route,
        elapsed_ms: pageCall.elapsedMs,
        anchor_message_id: anchor || null,
        anchor_message_seq: anchorSeq || null,
        message_count: rawPage.length,
      });
    }
    if (rawPage.length === 0) {
      exhausted = true;
      break;
    }
    pagesScanned += 1;
    const sortedPage = sortHistoryMessages(rawPage);
    let added = 0;
    const dedupedPage = [];
    for (const message of sortedPage) {
      const key = rawHistoryKey(message);
      if (!key || seenKeys.has(key)) {
        continue;
      }
      seenKeys.add(key);
      dedupedPage.push(message);
      added += 1;
    }
    if (dedupedPage.length > 0) {
      pageSegments.push(dedupedPage);
      collectedCount += dedupedPage.length;
    }
    if (added === 0) {
      noProgressStreak += 1;
      if (noProgressStreak >= 2) {
        exhausted = true;
        break;
      }
    } else {
      noProgressStreak = 0;
    }
    const oldest = sortedPage[0];
    const nextAnchor = rawHistoryAnchor(oldest);
    const nextAnchorSeq = rawHistorySeq(oldest);
    if (!nextAnchor || seenAnchors.has(nextAnchor)) {
      exhausted = true;
      break;
    }
    seenAnchors.add(nextAnchor);
    anchor = nextAnchor;
    anchorSeq = nextAnchorSeq || anchorSeq;
  }

  const orderedCollected = [];
  for (let index = pageSegments.length - 1; index >= 0; index -= 1) {
    orderedCollected.push(...pageSegments[index]);
  }
  const selected = orderedCollected.slice(-requestedDataCount);
  const nextAnchor = selected.length > 0 ? rawHistoryAnchor(selected[0]) : "";
  const nextAnchorSeq = selected.length > 0 ? rawHistorySeq(selected[0]) : "";
  return {
    chat_type: chatType,
    chat_id: chatId,
    requested_data_count: requestedDataCount,
    start_anchor_message_id: startAnchorMessageId || null,
    start_anchor_message_seq: startAnchorMessageSeq || null,
    page_size: pageSize,
    pages_scanned: pagesScanned,
    count: selected.length,
    history_fetch_strategy: historyFetchStrategy,
    next_anchor: nextAnchor || null,
    next_anchor_message_seq: nextAnchorSeq || null,
    messages_sorted_ascending: true,
    exhausted: exhausted || selected.length < requestedDataCount,
    page_call_breakdown: includeDebugStats ? chunkStats : undefined,
    messages: selected.map(slimRawMessage),
  };
}

async function fetchHistoryFullBulk(ctx, payload) {
  const chatType = payload?.chat_type === "group" ? "group" : "private";
  const chatId = asText(payload?.chat_id || payload?.group_id || payload?.user_id);
  if (!chatId) {
    throw new Error("chat_id is required");
  }
  const requestedDataCount = clampPositiveCount(
    payload?.data_count || payload?.requested_data_count || payload?.count,
    64000,
  );
  const pageSize = clampCount(payload?.page_size || payload?.count || DEFAULT_PAGE_SIZE);
  const historyFetchStrategy = normalizeHistoryFetchStrategy(
    payload?.history_fetch_strategy || payload?.strategy,
  );
  const includeDebugStats = asBool(payload?.include_debug_stats, false);
  const peer = await buildPeer(ctx, chatType, chatId);
  const seenKeys = new Set();
  const seenAnchors = new Set();
  const pageSegments = [];
  let anchor = "";
  let anchorSeq = "";
  let pagesScanned = 0;
  let noProgressStreak = 0;
  let exhausted = false;
  const chunkStats = [];
  const startedAt = Date.now();
  let collectedCount = 0;

  while (collectedCount < requestedDataCount) {
    const pageCall = await fetchHistoryWindow(
      ctx,
      peer,
      pageSize,
      historyFetchStrategy,
      anchor,
      anchorSeq,
    );
    const response = pageCall.response;
    const rawPage = Array.isArray(response?.msgList) ? response.msgList : [];
    if (includeDebugStats) {
      chunkStats.push({
        chunk_index: chunkStats.length + 1,
        route: pageCall.route,
        elapsed_ms: pageCall.elapsedMs,
        anchor_message_id: anchor || null,
        anchor_message_seq: anchorSeq || null,
        message_count: rawPage.length,
      });
    }
    if (rawPage.length === 0) {
      exhausted = true;
      break;
    }
    pagesScanned += 1;
    const sortedPage = sortHistoryMessages(rawPage);
    let added = 0;
    const dedupedPage = [];
    for (const message of sortedPage) {
      const key = rawHistoryKey(message);
      if (!key || seenKeys.has(key)) {
        continue;
      }
      seenKeys.add(key);
      dedupedPage.push(message);
      added += 1;
      if ((collectedCount + dedupedPage.length) >= requestedDataCount) {
        break;
      }
    }
    if (dedupedPage.length > 0) {
      pageSegments.push(dedupedPage);
      collectedCount += dedupedPage.length;
    }
    if (added === 0) {
      noProgressStreak += 1;
      if (noProgressStreak >= 2) {
        exhausted = true;
        break;
      }
    } else {
      noProgressStreak = 0;
    }
    const oldest = sortedPage[0];
    const nextAnchor = rawHistoryAnchor(oldest);
    const nextAnchorSeq = rawHistorySeq(oldest);
    if (!nextAnchor || seenAnchors.has(nextAnchor)) {
      exhausted = true;
      break;
    }
    seenAnchors.add(nextAnchor);
    anchor = nextAnchor;
    anchorSeq = nextAnchorSeq || anchorSeq;
  }

  const orderedCollected = [];
  for (let index = pageSegments.length - 1; index >= 0; index -= 1) {
    orderedCollected.push(...pageSegments[index]);
  }
  return {
    chat_type: chatType,
    chat_id: chatId,
    requested_data_count: requestedDataCount,
    page_size: pageSize,
    pages_scanned: pagesScanned,
    count: orderedCollected.length,
    history_fetch_strategy: historyFetchStrategy,
    next_anchor: orderedCollected.length > 0 ? rawHistoryAnchor(orderedCollected[0]) || null : null,
    next_anchor_message_seq: orderedCollected.length > 0 ? rawHistorySeq(orderedCollected[0]) || null : null,
    messages_sorted_ascending: true,
    exhausted: exhausted || orderedCollected.length < requestedDataCount,
    page_call_breakdown: includeDebugStats ? chunkStats : undefined,
    elapsed_ms: Math.max(0, Date.now() - startedAt),
    messages: orderedCollected.map(slimRawMessage),
  };
}

function buildRawPeer(peerUid, rawChatType) {
  return {
    chatType: rawChatType,
    peerUid,
    guildId: "",
  };
}

function buildPeerFromRawMessage(rawMessage) {
  return buildRawPeer(asText(rawMessage?.peerUid), Number(rawMessage?.chatType || 0));
}

function getFileTokenManager() {
  return globalThis.__NAPCAT_FILE_UUID__ || null;
}

function buildMarketFaceRemote(emojiId) {
  const safeEmojiId = asText(emojiId);
  if (safeEmojiId.length < 2) {
    return { remoteUrl: "", remoteFileName: "" };
  }
  const prefix = safeEmojiId.substring(0, 2);
  return {
    remoteUrl: `https://gxh.vip.qq.com/club/item/parcel/item/${prefix}/${safeEmojiId}/raw300.gif`,
    remoteFileName: `${prefix}-${safeEmojiId}.gif`,
  };
}

function buildPublicMediaAccess(rawMessage, element, normalizedAssetType, assetRole = "") {
  const manager = getFileTokenManager();
  if (!manager || !rawMessage || !element) {
    return { public_action: "", public_file_token: "" };
  }
  const peer = buildRawPeer(asText(rawMessage?.peerUid), Number(rawMessage?.chatType || 0));
  const msgId = asText(rawMessage?.msgId);
  const elementId = asText(element?.elementId);
  if (!peer.peerUid || !peer.chatType || !msgId || !elementId) {
    return { public_action: "", public_file_token: "" };
  }
  try {
    if (element?.picElement && normalizedAssetType === "image") {
      return {
        public_action: "get_image",
        public_file_token: manager.encode(
          peer,
          msgId,
          elementId,
          asText(element.picElement.fileUuid),
        ),
      };
    }
    if (element?.fileElement && normalizedAssetType === "file") {
      return {
        public_action: "get_file",
        public_file_token: manager.encode(
          peer,
          msgId,
          elementId,
          asText(element.fileElement.fileUuid),
        ),
      };
    }
    if (element?.videoElement && normalizedAssetType === "video") {
      return {
        public_action: "get_file",
        public_file_token: manager.encode(
          peer,
          msgId,
          elementId,
          asText(element.videoElement.fileUuid),
        ),
      };
    }
    if (element?.pttElement && normalizedAssetType === "speech") {
      return {
        public_action: "get_record",
        public_file_token: manager.encode(
          peer,
          msgId,
          elementId,
          "",
          asText(element.pttElement.fileName),
        ),
      };
    }
    if (element?.marketFaceElement && normalizedAssetType === "sticker") {
      const { remoteFileName } = buildMarketFaceRemote(element.marketFaceElement.emojiId);
      return {
        public_action: "get_image",
        public_file_token: manager.encode(
          peer,
          msgId,
          elementId,
          "",
          remoteFileName,
        ),
      };
    }
  } catch (_error) {
    return { public_action: "", public_file_token: "" };
  }
  return { public_action: "", public_file_token: "" };
}

function normalizeMatchUrl(value) {
  const text = asText(value);
  if (!text) {
    return "";
  }
  try {
    const parsed = new URL(text);
    parsed.hash = "";
    parsed.protocol = parsed.protocol.toLowerCase();
    parsed.hostname = parsed.hostname.toLowerCase();
    return parsed.toString();
  } catch (_error) {
    return text.toLowerCase();
  }
}

function normalizedFileStem(value) {
  const text = asText(value).toLowerCase();
  if (!text) {
    return "";
  }
  const fileName = text.split(/[\\/]/).pop() || text;
  const lastDot = fileName.lastIndexOf(".");
  return lastDot > 0 ? fileName.slice(0, lastDot) : fileName;
}

function buildForwardAssetTarget(payload) {
  const assetType = asText(payload?.asset_type).toLowerCase();
  const assetRole = asText(payload?.asset_role).toLowerCase();
  const fileName = asText(payload?.file_name).toLowerCase();
  const md5 = asText(payload?.md5).toLowerCase();
  const fileId = asText(payload?.file_id);
  const url = normalizeMatchUrl(payload?.url || payload?.remote_url);
  if (!assetType && !assetRole && !fileName && !md5 && !fileId && !url) {
    return null;
  }
  return {
    asset_type: assetType,
    asset_role: assetRole,
    file_name: fileName,
    md5,
    file_id: fileId,
    url,
    stem: normalizedFileStem(fileName),
  };
}

function buildForwardMatchCandidates(element) {
  if (element?.picElement) {
    return [{
      asset_type: "image",
      asset_role: "",
      file_name: asText(element.picElement.fileName).toLowerCase(),
      md5: asText(element.picElement.md5HexStr).toLowerCase(),
      file_id: asText(element.picElement.fileUuid),
      url: normalizeMatchUrl(
        asText(element.picElement.sourcePath) ||
        asText(element.picElement.filePath) ||
        asText(element.picElement.originImageUrl),
      ),
      stem: normalizedFileStem(element.picElement.fileName),
    }];
  }
  if (element?.videoElement) {
    return [{
      asset_type: "video",
      asset_role: "",
      file_name: asText(element.videoElement.fileName).toLowerCase(),
      md5: asText(element.videoElement.md5HexStr).toLowerCase(),
      file_id: asText(element.videoElement.fileUuid),
      url: normalizeMatchUrl(asText(element.videoElement.filePath)),
      stem: normalizedFileStem(element.videoElement.fileName),
    }];
  }
  if (element?.fileElement) {
    return [{
      asset_type: "file",
      asset_role: "",
      file_name: asText(element.fileElement.fileName).toLowerCase(),
      md5: asText(element.fileElement.fileMd5).toLowerCase(),
      file_id: asText(element.fileElement.fileUuid),
      url: normalizeMatchUrl(asText(element.fileElement.filePath)),
      stem: normalizedFileStem(element.fileElement.fileName),
    }];
  }
  if (element?.pttElement) {
    return [{
      asset_type: "speech",
      asset_role: "",
      file_name: asText(element.pttElement.fileName).toLowerCase(),
      md5: asText(element.pttElement.md5HexStr).toLowerCase(),
      file_id: asText(element.pttElement.fileUuid),
      url: normalizeMatchUrl(asText(element.pttElement.filePath)),
      stem: normalizedFileStem(element.pttElement.fileName),
    }];
  }
  if (element?.marketFaceElement) {
    const { remoteUrl, remoteFileName } = buildMarketFaceRemote(element.marketFaceElement.emojiId);
    const staticFacePath = asText(element.marketFaceElement.staticFacePath);
    const dynamicFacePath = asText(element.marketFaceElement.dynamicFacePath);
    const baseName = asText(element.marketFaceElement.faceName || remoteFileName).toLowerCase();
    return [
      {
        asset_type: "sticker",
        asset_role: "static",
        file_name: baseName,
        md5: "",
        file_id: "",
        url: normalizeMatchUrl(staticFacePath || remoteUrl),
        stem: normalizedFileStem(baseName),
      },
      {
        asset_type: "sticker",
        asset_role: "dynamic",
        file_name: baseName,
        md5: "",
        file_id: "",
        url: normalizeMatchUrl(dynamicFacePath || remoteUrl),
        stem: normalizedFileStem(baseName),
      },
    ];
  }
  return [];
}

function scoreForwardAssetCandidate(target, candidate) {
  if (!target || !candidate) {
    return 0;
  }
  if (target.asset_type && candidate.asset_type !== target.asset_type) {
    return 0;
  }
  if (target.asset_role && candidate.asset_role !== target.asset_role) {
    return 0;
  }
  let score = 0;
  if (target.md5 && candidate.md5 && candidate.md5 === target.md5) {
    score += 100;
  }
  if (target.file_id && candidate.file_id && candidate.file_id === target.file_id) {
    score += 90;
  }
  if (target.url && candidate.url && candidate.url === target.url) {
    score += 70;
  }
  if (target.file_name && candidate.file_name && candidate.file_name === target.file_name) {
    score += 50;
  }
  if (target.stem && candidate.stem && candidate.stem === target.stem) {
    score += 20;
  }
  return score;
}

function isDecisiveForwardAssetScore(target, candidate, score) {
  if (!target || !candidate || score <= 0) {
    return false;
  }
  if (target.md5 && candidate.md5 && candidate.md5 === target.md5) {
    return true;
  }
  if (target.file_id && candidate.file_id && candidate.file_id === target.file_id) {
    return true;
  }
  if (target.url && candidate.url && candidate.url === target.url) {
    return !target.file_name || !candidate.file_name || candidate.file_name === target.file_name;
  }
  return false;
}

async function findForwardMediaTarget(ctx, messages, target, depth = 1, forwardLoadCache = null) {
  let bestMatch = null;
  let bestScore = -1;
  for (const msg of messages || []) {
    const safeMsgId = asText(msg?.msgId);
    if (!safeMsgId || !Array.isArray(msg?.elements)) {
      continue;
    }
    for (const element of msg.elements) {
      const candidates = buildForwardMatchCandidates(element);
      for (const candidate of candidates) {
        const score = scoreForwardAssetCandidate(target, candidate);
        if (score <= 0 || score < bestScore) {
          continue;
        }
        bestMatch = {
          score,
          rawMessage: msg,
          element,
          depth,
          assetRole: candidate.asset_role || "",
        };
        bestScore = score;
        if (isDecisiveForwardAssetScore(target, candidate, score)) {
          return bestMatch;
        }
      }
      if (element?.multiForwardMsgElement?.resId) {
        const nested = await loadForwardMessages(ctx, msg, element.multiForwardMsgElement, forwardLoadCache);
        const nestedMatch = await findForwardMediaTarget(ctx, nested, target, depth + 1, forwardLoadCache);
        if (nestedMatch && nestedMatch.score > bestScore) {
          bestMatch = nestedMatch;
          bestScore = nestedMatch.score;
          if (nestedMatch.score >= 100) {
            return bestMatch;
          }
        }
      }
    }
  }
  return bestMatch;
}

function buildRawMessageContextCacheKey(payload) {
  const msgId = asText(payload?.message_id || payload?.message_id_raw);
  const peerUid = asText(payload?.peer_uid);
  const rawChatType = Number(payload?.chat_type_raw || 0);
  if (!msgId || !peerUid || !Number.isFinite(rawChatType) || rawChatType <= 0) {
    return "";
  }
  return `${rawChatType}:${peerUid}:${msgId}`;
}

async function getRawMessageByContext(ctx, payload, rawMessageCache = null) {
  const msgId = asText(payload?.message_id || payload?.message_id_raw);
  const peerUid = asText(payload?.peer_uid);
  const rawChatType = Number(payload?.chat_type_raw || 0);
  if (!msgId || !peerUid || !Number.isFinite(rawChatType) || rawChatType <= 0) {
    throw new Error("message_id_raw, peer_uid, and chat_type_raw are required");
  }
  const cacheKey = buildRawMessageContextCacheKey(payload);
  if (rawMessageCache instanceof Map && cacheKey) {
    const cached = rawMessageCache.get(cacheKey);
    if (cached) {
      return await cached;
    }
  }
  const peer = buildRawPeer(peerUid, rawChatType);
  const loadTask = (async () => {
    const rawMessage = (await ctx.core.apis.MsgApi.getMsgsByMsgId(peer, [msgId]))?.msgList?.find(
      (msg) => asText(msg?.msgId) === msgId,
    );
    if (!rawMessage) {
      throw new Error(`message ${msgId} not found`);
    }
    return { msgId, rawChatType, peer, rawMessage };
  })();
  if (rawMessageCache instanceof Map && cacheKey) {
    rawMessageCache.set(cacheKey, loadTask);
  }
  const resolved = await loadTask;
  if (rawMessageCache instanceof Map && cacheKey) {
    rawMessageCache.set(cacheKey, resolved);
  }
  return resolved;
}

function normalizeHydratedElementRecord(rawMessage, element, downloadPath, assetRole, depth = 0) {
  const marketFace = element?.marketFaceElement;
  const mixElementInner =
    element?.videoElement ??
    element?.fileElement ??
    element?.pttElement ??
    element?.picElement ??
    marketFace;
  let downloadFile = downloadPath;
  let normalizedAssetType = "file";
  let normalizedAssetRole = "";
  let remoteUrl = "";
  let fileId = "";
  if (element?.picElement) {
    normalizedAssetType = "image";
    downloadFile = downloadFile || asText(element.picElement.sourcePath) || asText(element.picElement.filePath);
    remoteUrl = asText(element.picElement.originImageUrl);
    fileId = asText(element.picElement.fileUuid);
  } else if (element?.videoElement) {
    normalizedAssetType = "video";
    downloadFile = downloadFile || asText(element.videoElement.filePath);
    fileId = asText(element.videoElement.fileUuid);
  } else if (element?.pttElement) {
    normalizedAssetType = "speech";
    downloadFile = downloadFile || asText(element.pttElement.filePath);
    fileId = asText(element.pttElement.fileUuid);
  } else if (element?.fileElement) {
    normalizedAssetType = "file";
    downloadFile = downloadFile || asText(element.fileElement.filePath);
    fileId = asText(element.fileElement.fileUuid);
  } else if (marketFace) {
    normalizedAssetType = "sticker";
    const { remoteUrl: marketFaceRemoteUrl, remoteFileName: marketFaceRemoteFileName } =
      buildMarketFaceRemote(marketFace?.emojiId);
    const staticFacePath = asText(marketFace?.staticFacePath);
    const dynamicFacePath = asText(marketFace?.dynamicFacePath);
    if (assetRole === "static" && staticFacePath) {
      downloadFile = staticFacePath;
      normalizedAssetRole = "static";
    } else if (assetRole === "dynamic" && dynamicFacePath) {
      downloadFile = dynamicFacePath;
      normalizedAssetRole = "dynamic";
    } else {
      downloadFile = dynamicFacePath || staticFacePath || downloadPath;
      normalizedAssetRole = dynamicFacePath ? "dynamic" : staticFacePath ? "static" : "";
    }
    remoteUrl = marketFaceRemoteUrl;
    fileId = "";
    if (!downloadFile && marketFaceRemoteFileName) {
      downloadFile = "";
    }
  }
  const fileSize = mixElementInner?.fileSize?.toString?.() ?? "";
  const fileName =
    asText(mixElementInner?.fileName) ||
    asText(marketFace?.faceName) ||
    asText(downloadFile?.split?.(/[\\/]/).pop());
  const md5 =
    asText(mixElementInner?.md5HexStr) ||
    asText(mixElementInner?.fileMd5) ||
    "";
  const publicAccess = buildPublicMediaAccess(rawMessage, element, normalizedAssetType, normalizedAssetRole);
  const { remoteFileName } = marketFace ? buildMarketFaceRemote(marketFace?.emojiId) : { remoteFileName: "" };
  return {
    asset_type: normalizedAssetType,
    asset_role: normalizedAssetRole,
    file: downloadFile,
    url: downloadFile || remoteUrl,
    remote_url: remoteUrl,
    remote_file_name: remoteFileName,
    file_name: fileName || remoteFileName,
    file_size: fileSize,
    md5,
    file_id: fileId,
    element_id: asText(element?.elementId),
    depth,
    public_action: publicAccess?.public_action ?? "",
    public_file_token: publicAccess?.public_file_token ?? "",
  };
}

function normalizeDownloadTimeoutMs(payload) {
  const raw = Number(payload?.download_timeout_ms);
  if (!Number.isFinite(raw)) {
    return 1e3 * 20;
  }
  return Math.max(1e3, Math.min(Math.trunc(raw), 1e3 * 60 * 2));
}

async function hydrateElementRecord(ctx, rawMessage, element, assetRole = "", depth = 0, options = {}) {
  const msgId = asText(rawMessage?.msgId);
  const elementId = asText(element?.elementId);
  const peer = buildPeerFromRawMessage(rawMessage);
  let downloadPath = "";
  let hydratedElement = element;
  if (element?.marketFaceElement) {
    return normalizeHydratedElementRecord(rawMessage, hydratedElement, downloadPath, assetRole, depth);
  }
  if (msgId && elementId && peer.peerUid && peer.chatType) {
    const downloadTimeoutMs = normalizeDownloadTimeoutMs(options);
    downloadPath = await ctx.core.apis.FileApi.downloadMedia(
      msgId,
      peer.chatType,
      peer.peerUid,
      elementId,
      "",
      "",
      downloadTimeoutMs,
      true,
    ).catch(() => "");
    const refreshedMessage = (await ctx.core.apis.MsgApi.getMsgsByMsgId(peer, [msgId]).catch(() => null))
      ?.msgList?.find((msg) => asText(msg?.msgId) === msgId);
    hydratedElement =
      refreshedMessage?.elements?.find((candidate) => asText(candidate?.elementId) === elementId) ||
      hydratedElement;
  }
  return normalizeHydratedElementRecord(rawMessage, hydratedElement, downloadPath, assetRole, depth);
}

async function hydrateMedia(ctx, payload) {
  const elementId = asText(payload?.element_id);
  const assetRole = asText(payload?.asset_role).toLowerCase();
  const metadataOnly = Boolean(payload?.metadata_only);
  if (!elementId) {
    throw new Error("element_id is required");
  }
  const { msgId, peer, rawMessage } = await getRawMessageByContext(ctx, payload);
  const mixElement = rawMessage?.elements?.find((element) => asText(element?.elementId) === elementId);
  if (!mixElement) {
    throw new Error(`element ${elementId} not found in message ${msgId}`);
  }
  if (metadataOnly) {
    return normalizeHydratedElementRecord(rawMessage, mixElement, "", assetRole);
  }
  return hydrateElementRecord(ctx, rawMessage, mixElement, assetRole);
}

async function hydrateMediaBatch(ctx, payload) {
  const items = Array.isArray(payload?.items) ? payload.items : [];
  const results = [];
  for (const item of items) {
    try {
      const data = await hydrateMedia(ctx, item || {});
      results.push({ ok: true, data });
    } catch (error) {
      results.push({
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }
  return { items: results };
}

function buildForwardLoadCacheKey(rawMessage, forwardElement) {
  const resId = asText(forwardElement?.resId);
  const msgId = asText(rawMessage?.msgId);
  if (!resId && !msgId) {
    return "";
  }
  return `${resId || "-"}::${msgId || "-"}`;
}

async function loadForwardMessages(ctx, rawMessage, forwardElement, forwardLoadCache = null) {
  const cacheKey = buildForwardLoadCacheKey(rawMessage, forwardElement);
  if (forwardLoadCache instanceof Map && cacheKey) {
    const cached = forwardLoadCache.get(cacheKey);
    if (cached) {
      return await cached;
    }
  }
  const loadTask = (async () => {
  const parentMsgPeer = rawMessage?.parentMsgPeer ?? {
    chatType: rawMessage?.chatType,
    guildId: "",
    peerUid: rawMessage?.peerUid,
  };
  const parentMsgIdList = Array.isArray(rawMessage?.parentMsgIdList)
    ? [...rawMessage.parentMsgIdList]
    : [];
  parentMsgIdList.push(rawMessage?.msgId);
  if (parentMsgIdList[0]) {
    try {
      const multiMsgs = (
        await ctx.core.apis.MsgApi.getMultiMsg(parentMsgPeer, parentMsgIdList[0], rawMessage?.msgId)
      )?.msgList;
      if (Array.isArray(multiMsgs) && multiMsgs.length > 0) {
        return multiMsgs;
      }
    } catch (error) {
      ctx.logger.warn(
        `loadForwardMessages getMultiMsg failed msgId=${asText(rawMessage?.msgId) || "-"} detail=${
          error instanceof Error ? error.message : String(error)
        }`,
      );
    }
  }
  try {
    const fallback = await ctx.core.apis.PacketApi.pkt.operation.FetchForwardMsg(forwardElement?.resId);
    return Array.isArray(fallback) ? fallback : [];
  } catch (error) {
    ctx.logger.warn(
      `loadForwardMessages fallback failed resId=${asText(forwardElement?.resId) || "-"} detail=${
        error instanceof Error ? error.message : String(error)
      }`,
    );
    return [];
  }
  })();
  if (forwardLoadCache instanceof Map && cacheKey) {
    forwardLoadCache.set(cacheKey, loadTask);
  }
  const loaded = await loadTask;
  if (forwardLoadCache instanceof Map && cacheKey) {
    forwardLoadCache.set(cacheKey, loaded);
  }
  return loaded;
}

async function collectForwardMediaRecords(ctx, messages, depth = 1, forwardLoadCache = null, options = {}) {
  const metadataOnly = Boolean(options?.metadataOnly);
  const assets = [];
  for (const msg of messages || []) {
    const safeMsgId = asText(msg?.msgId);
    if (!safeMsgId || !Array.isArray(msg?.elements)) {
      continue;
    }
    for (const element of msg.elements) {
      if (element?.picElement || element?.fileElement || element?.videoElement || element?.pttElement) {
        const normalized = metadataOnly
          ? normalizeHydratedElementRecord(msg, element, "", "", depth)
          : await hydrateElementRecord(ctx, msg, element, "", depth);
        assets.push({
          ...normalized,
          source_message_id_raw: safeMsgId,
          source_sender_uin: asText(msg?.senderUin),
        });
        continue;
      }
      if (element?.marketFaceElement) {
        const staticRecord = metadataOnly
          ? normalizeHydratedElementRecord(msg, element, "", "static", depth)
          : await hydrateElementRecord(ctx, msg, element, "static", depth);
        const dynamicRecord = metadataOnly
          ? normalizeHydratedElementRecord(msg, element, "", "dynamic", depth)
          : await hydrateElementRecord(ctx, msg, element, "dynamic", depth);
        if (asText(staticRecord.file)) {
          assets.push({
            ...staticRecord,
            source_message_id_raw: safeMsgId,
            source_sender_uin: asText(msg?.senderUin),
          });
        }
        if (asText(dynamicRecord.file) && asText(dynamicRecord.file) !== asText(staticRecord.file)) {
          assets.push({
            ...dynamicRecord,
            source_message_id_raw: safeMsgId,
            source_sender_uin: asText(msg?.senderUin),
          });
        }
        continue;
      }
      if (element?.multiForwardMsgElement?.resId) {
        const nested = await loadForwardMessages(ctx, msg, element.multiForwardMsgElement, forwardLoadCache);
        const nestedAssets = await collectForwardMediaRecords(
          ctx,
          nested,
          depth + 1,
          forwardLoadCache,
          { metadataOnly },
        );
        assets.push(...nestedAssets);
      }
    }
  }
  return assets;
}

async function hydrateForwardMedia(ctx, payload) {
  const elementId = asText(payload?.element_id);
  if (!elementId) {
    throw new Error("element_id is required");
  }
  const { rawMessage } = await getRawMessageByContext(ctx, payload);
  const forwardElement = rawMessage?.elements?.find(
    (element) => asText(element?.elementId) === elementId && element?.multiForwardMsgElement?.resId,
  );
  if (!forwardElement?.multiForwardMsgElement?.resId) {
    throw new Error(`forward element ${elementId} not found`);
  }
  const forwardLoadCache = new Map();
  const nestedMessages = await loadForwardMessages(ctx, rawMessage, forwardElement.multiForwardMsgElement, forwardLoadCache);
    const target = buildForwardAssetTarget(payload);
    const hasLocatorEvidence = Boolean(
      target && (target.file_name || target.md5 || target.file_id || target.url || target.stem)
    );
    if (target && hasLocatorEvidence) {
      const matched = await findForwardMediaTarget(ctx, nestedMessages, target, 1, forwardLoadCache);
      if (matched?.rawMessage && matched?.element) {
        const matchedAssetType = asText(target?.asset_type).toLowerCase();
        const shouldMaterialize = Boolean(payload?.materialize);
        // Forward file/video assets often already expose enough authoritative
        // metadata (file path hint, fileUuid, public token context) to let Python
        // drive the actual download. Avoid blocking the whole export on a long
        // downloadMedia(...) call before returning that metadata.
        const metadataOnly =
          !shouldMaterialize
          && (matchedAssetType === "image" || matchedAssetType === "video" || matchedAssetType === "file");
        const normalized = metadataOnly
          ? normalizeHydratedElementRecord(
              matched.rawMessage,
              matched.element,
              "",
              matched.assetRole || "",
              matched.depth || 1,
            )
          : await hydrateElementRecord(
              ctx,
              matched.rawMessage,
              matched.element,
              matched.assetRole || "",
              matched.depth || 1,
              { download_timeout_ms: payload?.download_timeout_ms },
            );
        return {
          assets: [{
            ...normalized,
            source_message_id_raw: asText(matched.rawMessage?.msgId),
            source_sender_uin: asText(matched.rawMessage?.senderUin),
          }],
          targeted: true,
          targeted_mode: metadataOnly
            ? "metadata_only"
            : (matchedAssetType === "video" || matchedAssetType === "file")
              ? "single_target_download"
              : "hydrated",
        };
      }
      return {
        assets: [],
        targeted: true,
        targeted_mode: "targeted_miss",
      };
    }
  const metadataOnly = !Boolean(payload?.materialize);
  let assets = await collectForwardMediaRecords(
    ctx,
    nestedMessages,
    1,
    forwardLoadCache,
    { metadataOnly },
  );
  if (target) {
    assets = assets.filter((asset) => {
      const assetType = asText(asset?.asset_type).toLowerCase();
      const assetRole = asText(asset?.asset_role).toLowerCase();
      if (target.asset_type && assetType !== target.asset_type) {
        return false;
      }
      if (target.asset_role && assetRole !== target.asset_role) {
        return false;
      }
      return true;
    });
  }
  return {
    assets,
    targeted: false,
    targeted_mode: metadataOnly ? "metadata_parent_scope" : "",
  };
}

async function hydrateForwardDetail(ctx, payload, options = {}) {
  const elementId = asText(payload?.element_id);
  if (!elementId) {
    throw new Error("element_id is required");
  }
  const rawMessageCache = options?.rawMessageCache instanceof Map ? options.rawMessageCache : null;
  const forwardLoadCache = options?.forwardLoadCache instanceof Map ? options.forwardLoadCache : new Map();
  const { rawMessage } = await getRawMessageByContext(ctx, payload, rawMessageCache);
  const forwardElement = rawMessage?.elements?.find(
    (element) => asText(element?.elementId) === elementId && element?.multiForwardMsgElement?.resId,
  );
  if (!forwardElement?.multiForwardMsgElement?.resId) {
    throw new Error(`forward element ${elementId} not found`);
  }
  const nestedMessages = await loadForwardMessages(
    ctx,
    rawMessage,
    forwardElement.multiForwardMsgElement,
    forwardLoadCache,
  );
  return {
    targeted: true,
    forward_id: asText(forwardElement.multiForwardMsgElement?.resId) || null,
    message_count: Array.isArray(nestedMessages) ? nestedMessages.length : 0,
    messages: Array.isArray(nestedMessages) ? nestedMessages.map(slimRawMessage) : [],
  };
}

async function hydrateForwardDetailBatch(ctx, payload) {
  const items = Array.isArray(payload?.items) ? payload.items : [];
  const rawMessageCache = new Map();
  const forwardLoadCache = new Map();
  const results = await Promise.all(items.map(async (item) => {
    try {
      const data = await hydrateForwardDetail(ctx, item || {}, {
        rawMessageCache,
        forwardLoadCache,
      });
      return { ok: true, data };
    } catch (error) {
      return {
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      };
    }
  }));
  return { items: results };
}

export async function plugin_init(ctx) {
  ctx.logger.info("QQ Data Fast History plugin initialized");

  const capabilityRoutes = [
    { name: "health", method: "GET", path: "/health" },
    { name: "capabilities", method: "GET", path: "/capabilities" },
    { name: "history", method: "POST", path: "/history" },
    { name: "history_tail_bulk", method: "POST", path: "/history-tail-bulk" },
    { name: "history_full_bulk", method: "POST", path: "/history-full-bulk" },
    { name: "hydrate_media", method: "POST", path: "/hydrate-media" },
    { name: "hydrate_media_batch", method: "POST", path: "/hydrate-media-batch" },
    { name: "hydrate_forward_detail", method: "POST", path: "/hydrate-forward-detail" },
    { name: "hydrate_forward_detail_batch", method: "POST", path: "/hydrate-forward-detail-batch" },
    { name: "hydrate_forward_media", method: "POST", path: "/hydrate-forward-media" },
  ];

  ctx.router.getNoAuth("/health", (_req, res) => {
    res.json({
      code: 0,
      data: {
        ok: true,
        plugin: ctx.pluginName,
        version: "0.1.0",
      },
    });
  });

  ctx.router.getNoAuth("/capabilities", (_req, res) => {
    res.json({
      code: 0,
      data: {
        ok: true,
        plugin: ctx.pluginName,
        version: "0.1.0",
        features: {
          forward_detail_batch: true,
          forward_parent_metadata_scope: true,
          history_fetch_strategy: [
            HISTORY_FETCH_STRATEGY_MSG_HISTORY,
            HISTORY_FETCH_STRATEGY_SEQ_WINDOW,
            HISTORY_FETCH_STRATEGY_QUERY_SEQ_WINDOW,
          ],
        },
        routes: capabilityRoutes,
      },
    });
  });

  ctx.router.postNoAuth("/history", async (req, res) => {
    try {
      const data = await fetchHistory(ctx, req.body || {});
      res.json({ code: 0, data });
    } catch (error) {
      ctx.logger.error("history route failed", error);
      res.status(500).json({
        code: -1,
        message: error instanceof Error ? error.message : String(error),
      });
    }
  });

  ctx.router.postNoAuth("/history-tail-bulk", async (req, res) => {
    try {
      const data = await fetchHistoryTailBulk(ctx, req.body || {});
      res.json({ code: 0, data });
    } catch (error) {
      ctx.logger.error("history-tail-bulk route failed", error);
      res.status(500).json({
        code: -1,
        message: error instanceof Error ? error.message : String(error),
      });
    }
  });

  ctx.router.postNoAuth("/history-full-bulk", async (req, res) => {
    try {
      const data = await fetchHistoryFullBulk(ctx, req.body || {});
      res.json({ code: 0, data });
    } catch (error) {
      ctx.logger.error("history-full-bulk route failed", error);
      res.status(500).json({
        code: -1,
        message: error instanceof Error ? error.message : String(error),
      });
    }
  });

  ctx.router.postNoAuth("/hydrate-media", async (req, res) => {
    try {
      const data = await hydrateMedia(ctx, req.body || {});
      res.json({ code: 0, data });
    } catch (error) {
      ctx.logger.error("hydrate-media route failed", error);
      res.status(500).json({
        code: -1,
        message: error instanceof Error ? error.message : String(error),
      });
    }
  });

  ctx.router.postNoAuth("/hydrate-media-batch", async (req, res) => {
    try {
      const data = await hydrateMediaBatch(ctx, req.body || {});
      res.json({ code: 0, data });
    } catch (error) {
      ctx.logger.error("hydrate-media-batch route failed", error);
      res.status(500).json({
        code: -1,
        message: error instanceof Error ? error.message : String(error),
      });
    }
  });

  ctx.router.postNoAuth("/hydrate-forward-detail", async (req, res) => {
    try {
      const data = await hydrateForwardDetail(ctx, req.body || {});
      res.json({ code: 0, data });
    } catch (error) {
      ctx.logger.error("hydrate-forward-detail route failed", error);
      res.status(500).json({
        code: -1,
        message: error instanceof Error ? error.message : String(error),
      });
    }
  });

  ctx.router.postNoAuth("/hydrate-forward-detail-batch", async (req, res) => {
    try {
      const data = await hydrateForwardDetailBatch(ctx, req.body || {});
      res.json({ code: 0, data });
    } catch (error) {
      ctx.logger.error("hydrate-forward-detail-batch route failed", error);
      res.status(500).json({
        code: -1,
        message: error instanceof Error ? error.message : String(error),
      });
    }
  });

  ctx.router.postNoAuth("/hydrate-forward-media", async (req, res) => {
    try {
      const data = await hydrateForwardMedia(ctx, req.body || {});
      res.json({ code: 0, data });
    } catch (error) {
      ctx.logger.error("hydrate-forward-media route failed", error);
      res.status(500).json({
        code: -1,
        message: error instanceof Error ? error.message : String(error),
      });
    }
  });
}
