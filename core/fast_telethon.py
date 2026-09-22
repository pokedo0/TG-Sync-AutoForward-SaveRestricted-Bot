"""FastTelethon: 安全型多连接并发传输引擎。

吸收社区 Gist 评论区最佳实践：
1. 使用 Telethon 官方 _borrow_exported_sender / _return_exported_sender 借调连接池。
2. 严格的 try...finally 归还连接，杜绝连接泄漏与协程挂死。
3. 严格限制并发连接数在 [2, 8] 之间（默认 4），避开 Telegram DC 速率风控与 FLOOD_PREMIUM_WAIT。
4. 针对小文件与空文件进行除零保护与大小阈值分流（<10MB 直接抛出异常以便回退）。
5. 随机 Seek 原子写与进度追踪，精准记录耗时与平均传输速率 (MB/s)。
"""

import asyncio
import logging
import os
import time

from telethon import TelegramClient, errors, functions, helpers, types, utils
from telethon.network import MTProtoSender

logger = logging.getLogger("tg_forward_bot.fast_telethon")

PART_SIZE = 512 * 1024  # 512 KB 分片（Telegram MTProto 允许的最大分片）
MIN_PARALLEL_FILE_SIZE = 10 * 1024 * 1024  # 10 MB（低于此体积并发握手得不偿失，直接走原生）
MAX_CONNECTIONS = 8
MIN_CONNECTIONS = 2
DEFAULT_CONNECTIONS = 4


def clamp_connections(n: int) -> int:
    """约束并发连接数在 [MIN_CONNECTIONS, MAX_CONNECTIONS] 区间。"""
    try:
        val = int(n)
    except (ValueError, TypeError):
        val = DEFAULT_CONNECTIONS
    return max(MIN_CONNECTIONS, min(MAX_CONNECTIONS, val))


async def _acquire_senders(client: TelegramClient, dc_id: int, count: int) -> tuple[list, any]:
    """为指定 dc_id 获取 count 个可用的 MTProtoSender。

    1. 若 dc_id == client.session.dc_id（同机房，例如上传到自身机房）：
       - Telegram 禁止向同一 DC 调用 ExportAuthorizationRequest。
       - 直接复用 client.session.auth_key 创建 count 个独立的 MTProtoSender 连接。
    2. 若 dc_id != client.session.dc_id（异地机房，例如从其他 DC 下载文件）：
       - 首个连接使用 client._borrow_exported_sender(dc_id) 借调并完成授权导出/导入。
       - 剩余的 count - 1 个连接复用首个借调 sender 的 auth_key 直连。
    """
    is_same_dc = (dc_id == client.session.dc_id)
    senders = []
    borrowed_sender = None

    try:
        if is_same_dc:
            dc = await client._get_dc(dc_id)
            for _ in range(count):
                sender = MTProtoSender(client.session.auth_key, loggers=client._log)
                await sender.connect(client._connection(
                    dc.ip_address,
                    dc.port,
                    dc.id,
                    loggers=client._log,
                    proxy=client._proxy,
                    local_addr=client._local_addr,
                ))
                senders.append(sender)
        else:
            borrowed_sender = await client._borrow_exported_sender(dc_id)
            senders.append(borrowed_sender)

            if count > 1 and getattr(borrowed_sender, "auth_key", None):
                dc = await client._get_dc(dc_id)
                for _ in range(count - 1):
                    sender = MTProtoSender(borrowed_sender.auth_key, loggers=client._log)
                    await sender.connect(client._connection(
                        dc.ip_address,
                        dc.port,
                        dc.id,
                        loggers=client._log,
                        proxy=client._proxy,
                        local_addr=client._local_addr,
                    ))
                    senders.append(sender)

        return senders, borrowed_sender
    except Exception:
        for s in senders:
            if s is not borrowed_sender:
                try:
                    await s.disconnect()
                except Exception:
                    pass
        if borrowed_sender:
            try:
                await client._return_exported_sender(borrowed_sender)
            except Exception:
                pass
        raise


async def _release_senders(client: TelegramClient, senders: list, borrowed_sender) -> None:
    """清理并断开所有额外创建的 sender，归还 borrowed_sender。"""
    for s in senders:
        if s is not borrowed_sender:
            try:
                await s.disconnect()
            except Exception as e:
                logger.debug("[FastTelethon] 断开额外 Sender 异常: %s", e)
    if borrowed_sender:
        try:
            await client._return_exported_sender(borrowed_sender)
        except Exception as e:
            logger.debug("[FastTelethon] 归还借调 Sender 异常: %s", e)


async def fast_download_file(
    client: TelegramClient,
    location,
    out_file_path: str,
    file_size: int | None = None,
    connection_count: int = DEFAULT_CONNECTIONS,
    progress_callback=None,
) -> str:
    """通过多连接并发分片高速下载 Telegram 媒体文件。

    :param client: TelegramClient 实例 (通常为 UserBot)
    :param location: 目标消息或媒体对象 (Message / Document / InputDocumentFileLocation 等)
    :param out_file_path: 本地目标存储路径
    :param file_size: 文件总字节大小 (若未提供则尝试自动解析)
    :param connection_count: 并发连接数 (推荐 4, 范围 2-8)
    :param progress_callback: 进度回调 (current_bytes, total_bytes)
    :return: 写入完成的本地绝对文件路径
    """
    start_time = time.monotonic()

    # 1. 前置快速检查（若已传入 file_size）
    if file_size is not None and file_size <= 0:
        raise ValueError("文件大小无效或为空，终止并发下载")
    if file_size is not None and file_size < MIN_PARALLEL_FILE_SIZE:
        raise ValueError(
            f"文件大小 ({file_size / (1024 * 1024):.2f} MB) 低于并发加速阈值 "
            f"({MIN_PARALLEL_FILE_SIZE / (1024 * 1024)} MB)，建议使用原生单连接"
        )

    # 2. 解析目标位置与所属 DC
    dc_id, input_location = utils.get_input_location(location)
    if dc_id is None:
        dc_id = client.session.dc_id

    # 3. 若未预传 file_size，则从 location 解析
    if file_size is None:
        if hasattr(location, "size") and location.size:
            file_size = location.size
        elif hasattr(location, "document") and getattr(location.document, "size", None):
            file_size = location.document.size
        elif hasattr(location, "media"):
            doc = getattr(location.media, "document", None)
            if doc and getattr(doc, "size", None):
                file_size = doc.size

    if not file_size or file_size <= 0:
        raise ValueError("无法解析文件大小或文件为空，不执行并发下载")

    if file_size < MIN_PARALLEL_FILE_SIZE:
        raise ValueError(
            f"文件大小 ({file_size / (1024 * 1024):.2f} MB) 低于并发加速阈值 "
            f"({MIN_PARALLEL_FILE_SIZE / (1024 * 1024)} MB)，建议使用原生单连接"
        )

    # 3. 计算分片与连接数
    part_count = (file_size + PART_SIZE - 1) // PART_SIZE
    if part_count == 0:
        raise ValueError("分片数为 0，终止下载")

    active_connections = min(clamp_connections(connection_count), part_count)

    # 4. 确保本地目录并预分配文件
    os.makedirs(os.path.dirname(os.path.abspath(out_file_path)), exist_ok=True)
    with open(out_file_path, "wb") as f:
        f.truncate(file_size)

    # 5. 准备并发任务队列
    queue = asyncio.Queue()
    for part_idx in range(part_count):
        offset = part_idx * PART_SIZE
        limit = min(PART_SIZE, file_size - offset)
        queue.put_nowait((part_idx, offset, limit))

    senders = []
    borrowed_sender = None
    file_lock = asyncio.Lock()
    downloaded_bytes = 0
    progress_lock = asyncio.Lock()

    logger.debug(
        "[FastTelethon] 开始并发下载: DC=%s, 大小=%.2f MB, 分片数=%d, 连接数=%d",
        dc_id, file_size / (1024 * 1024), part_count, active_connections,
    )

    try:
        senders, borrowed_sender = await _acquire_senders(client, dc_id, active_connections)

        with open(out_file_path, "r+b") as fh:
            async def worker(sender):
                nonlocal downloaded_bytes
                while True:
                    try:
                        part_idx, offset, limit = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                    res = None
                    last_err = None
                    for attempt in range(3):
                        try:
                            # limit 需为 512KB 对齐
                            res = await sender.send(
                                functions.upload.GetFileRequest(
                                    location=input_location,
                                    offset=offset,
                                    limit=PART_SIZE,
                                )
                            )
                            break
                        except errors.FloodWaitError as fe:
                            if fe.seconds <= 5:
                                logger.warning("[FastTelethon] DC=%s 限频等待 %s 秒", dc_id, fe.seconds)
                                await asyncio.sleep(fe.seconds)
                            else:
                                raise
                        except Exception as e:
                            last_err = e
                            if attempt == 2:
                                raise
                            await asyncio.sleep(0.5 * (attempt + 1))

                    if not res or not getattr(res, "bytes", None):
                        raise RuntimeError(
                            f"分片 #{part_idx} 下载失败或返回空数据: {last_err}"
                        )

                    chunk_data = res.bytes[:limit]
                    async with file_lock:
                        fh.seek(offset)
                        fh.write(chunk_data)

                    async with progress_lock:
                        downloaded_bytes += len(chunk_data)
                        if progress_callback:
                            try:
                                progress_callback(downloaded_bytes, file_size)
                            except Exception:
                                pass

                    queue.task_done()

            # 并发执行所有 worker
            tasks = [asyncio.create_task(worker(s)) for s in senders]
            await asyncio.gather(*tasks)

    finally:
        await _release_senders(client, senders, borrowed_sender)

    elapsed = max(0.001, time.monotonic() - start_time)
    speed_mb = (file_size / (1024 * 1024)) / elapsed
    logger.info(
        "[FastTelethon] 下载完成: 大小=%.2f MB, 耗时=%.2f 秒, 平均速率=%.2f MB/s",
        file_size / (1024 * 1024), elapsed, speed_mb,
    )
    return out_file_path


async def fast_upload_file(
    client: TelegramClient,
    file_path: str,
    connection_count: int = DEFAULT_CONNECTIONS,
    progress_callback=None,
) -> types.InputFileBig | types.InputFile:
    """通过多连接并发分片高速上传本地文件到 Telegram。

    :param client: TelegramClient 实例 (通常为 Bot 或 UserBot)
    :param file_path: 本地文件绝对路径
    :param connection_count: 并发连接数 (推荐 4, 范围 2-8)
    :param progress_callback: 进度回调 (current_bytes, total_bytes)
    :return: 适用于 send_file 的 InputFileBig 或 InputFile 对象
    """
    start_time = time.monotonic()

    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")

    file_size = os.path.getsize(file_path)
    if file_size <= 0:
        raise ValueError("文件为空，无法执行并发上传")

    if file_size < MIN_PARALLEL_FILE_SIZE:
        raise ValueError(
            f"文件大小 ({file_size / (1024 * 1024):.2f} MB) 低于并发加速阈值 "
            f"({MIN_PARALLEL_FILE_SIZE / (1024 * 1024)} MB)，建议使用原生单连接"
        )

    # 1. 基础分片计算
    part_count = (file_size + PART_SIZE - 1) // PART_SIZE
    is_large = file_size > 10 * 1024 * 1024  # >10MB 使用大文件协议 SaveBigFilePartRequest
    file_id = helpers.generate_random_long()
    file_name = os.path.basename(file_path)
    dc_id = client.session.dc_id
    active_connections = min(clamp_connections(connection_count), part_count)

    # 2. 准备分片队列
    queue = asyncio.Queue()
    for part_idx in range(part_count):
        offset = part_idx * PART_SIZE
        limit = min(PART_SIZE, file_size - offset)
        queue.put_nowait((part_idx, offset, limit))

    senders = []
    borrowed_sender = None
    file_read_lock = asyncio.Lock()
    uploaded_bytes = 0
    progress_lock = asyncio.Lock()

    logger.debug(
        "[FastTelethon] 开始并发上传: DC=%s, 大小=%.2f MB, 分片数=%d, 连接数=%d, is_large=%s",
        dc_id, file_size / (1024 * 1024), part_count, active_connections, is_large,
    )

    try:
        senders, borrowed_sender = await _acquire_senders(client, dc_id, active_connections)

        with open(file_path, "rb") as fh:
            async def worker(sender):
                nonlocal uploaded_bytes
                while True:
                    try:
                        part_idx, offset, limit = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                    # 读取分片
                    async with file_read_lock:
                        fh.seek(offset)
                        chunk_data = fh.read(limit)

                    if not chunk_data:
                        raise RuntimeError(f"读取分片 #{part_idx} 数据为空 (offset={offset})")

                    # 构造请求
                    if is_large:
                        req = functions.upload.SaveBigFilePartRequest(
                            file_id=file_id,
                            file_part=part_idx,
                            file_total_parts=part_count,
                            bytes=chunk_data,
                        )
                    else:
                        req = functions.upload.SaveFilePartRequest(
                            file_id=file_id,
                            file_part=part_idx,
                            bytes=chunk_data,
                        )

                    # 发送请求并重试
                    for attempt in range(3):
                        try:
                            await sender.send(req)
                            break
                        except errors.FloodWaitError as fe:
                            if fe.seconds <= 5:
                                logger.warning("[FastTelethon] 上传分片限频等待 %s 秒", fe.seconds)
                                await asyncio.sleep(fe.seconds)
                            else:
                                raise
                        except Exception as e:
                            err_str = str(e)
                            if "FLOOD_PREMIUM_WAIT" in err_str:
                                logger.error(
                                    "[FastTelethon] 触发 Telegram 服务端 FLOOD_PREMIUM_WAIT 风控: %s",
                                    err_str,
                                )
                                raise
                            if attempt == 2:
                                raise
                            await asyncio.sleep(0.5 * (attempt + 1))

                    async with progress_lock:
                        uploaded_bytes += len(chunk_data)
                        if progress_callback:
                            try:
                                progress_callback(uploaded_bytes, file_size)
                            except Exception:
                                pass

                    queue.task_done()

            # 并发执行所有 worker
            tasks = [asyncio.create_task(worker(s)) for s in senders]
            await asyncio.gather(*tasks)

    finally:
        await _release_senders(client, senders, borrowed_sender)

    elapsed = max(0.001, time.monotonic() - start_time)
    speed_mb = (file_size / (1024 * 1024)) / elapsed
    logger.info(
        "[FastTelethon] 上传完成: 大小=%.2f MB, 耗时=%.2f 秒, 平均速率=%.2f MB/s",
        file_size / (1024 * 1024), elapsed, speed_mb,
    )

    if is_large:
        return types.InputFileBig(id=file_id, parts=part_count, name=file_name)
    return types.InputFile(id=file_id, parts=part_count, name=file_name, md5_checksum="")
