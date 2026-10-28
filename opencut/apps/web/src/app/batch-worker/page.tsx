"use client";

import { useEffect, useState } from "react";
import { EditorCore } from "@/core";
import { loadFonts } from "@/fonts/google-fonts";
import type { MediaAsset } from "@/media/types";
import { initializeGpuRenderer } from "@/services/renderer/gpu-renderer";
import { exportTimeline } from "@/services/renderer/timeline-export";
import { videoCache } from "@/services/video-cache/service";
import { generateUUID } from "@/utils/id";
import {
	BATCH_CHANNEL,
	type BatchJob,
	type BatchMessage,
} from "@/batch/protocol";

/**
 * Trang xuất video chạy trong một cửa sổ riêng do app desktop mở.
 * Mỗi cửa sổ là một tiến trình trình duyệt riêng nên nhiều video được xuất song song thật.
 */
export default function BatchWorkerPage() {
	const [status, setStatus] = useState("Đang khởi động…");

	useEffect(() => {
		const workerId = generateUUID();
		const channel = new BroadcastChannel(BATCH_CHANNEL);
		const cancelled = new Set<string>();
		const post = (message: BatchMessage) => channel.postMessage(message);

		const runJob = async ({ job }: { job: BatchJob }) => {
			setStatus("Đang xuất…");
			const assets: MediaAsset[] = job.mediaAssets.map((asset) => ({
				...asset,
				url: URL.createObjectURL(asset.file),
			}));
			let lastPost = 0;
			try {
				await loadFonts({ families: job.fontFamilies });
				const writable = await job.output.createWritable();
				const result = await exportTimeline({
					tracks: job.tracks,
					mediaAssets: assets,
					settings: job.settings,
					options: job.options,
					stream: writable,
					onCancel: () => cancelled.has(job.jobId),
					onProgress: ({ progress }) => {
						const now = performance.now();
						if (now - lastPost < 250) return;
						lastPost = now;
						post({ type: "progress", jobId: job.jobId, progress });
					},
				});
				if (!result.success && !writable.locked) {
					await writable.abort().catch(() => {});
				}
				post({
					type: "result",
					jobId: job.jobId,
					success: result.success,
					cancelled: result.cancelled,
					error: result.error,
				});
			} catch (error) {
				post({
					type: "result",
					jobId: job.jobId,
					success: false,
					error: error instanceof Error ? error.message : String(error),
				});
			} finally {
				for (const asset of assets) {
					videoCache.clearVideo({ mediaId: asset.id });
					if (asset.url) URL.revokeObjectURL(asset.url);
				}
				setStatus("Đang chờ việc");
				post({ type: "worker-ready", workerId });
			}
		};

		channel.onmessage = (event: MessageEvent<BatchMessage>) => {
			const message = event.data;
			if (message.type === "hello") post({ type: "worker-ready", workerId });
			if (message.type === "cancel") cancelled.add(message.jobId);
			if (message.type === "job" && message.workerId === workerId) {
				void runJob({ job: message.job });
			}
		};

		// EditorCore đăng ký các hiệu ứng/mask mà bộ dựng hình cần
		EditorCore.getInstance();
		initializeGpuRenderer()
			.catch(() => {})
			.finally(() => {
				setStatus("Đang chờ việc");
				post({ type: "worker-ready", workerId });
			});

		return () => channel.close();
	}, []);

	return (
		<div className="text-muted-foreground flex h-screen items-center justify-center text-sm">
			OpenCut — {status}
		</div>
	);
}
