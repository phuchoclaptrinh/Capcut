import type { StreamTargetChunk } from "mediabunny";
import type { ExportOptions, ExportResult } from "@/export";
import { createTimelineAudioBuffer } from "@/media/audio";
import type { MediaAsset } from "@/media/types";
import type { TProjectSettings } from "@/project/types";
import { calculateTotalDuration, type SceneTracks } from "@/timeline";
import { buildScene } from "./scene-builder";
import { SceneExporter } from "./scene-exporter";

/**
 * Xuất một timeline bất kỳ. Không cần EditorCore nên chạy được cả trong
 * trang xuất riêng (batch worker).
 * Có `stream` thì ghi thẳng vào file, khi đó kết quả không kèm `buffer`.
 */
export async function exportTimeline({
	tracks,
	mediaAssets,
	settings,
	options,
	onProgress,
	onCancel,
	stream,
}: {
	tracks: SceneTracks;
	mediaAssets: MediaAsset[];
	settings: Pick<TProjectSettings, "fps" | "canvasSize" | "background">;
	options: ExportOptions;
	onProgress?: ({ progress }: { progress: number }) => void;
	onCancel?: () => boolean;
	stream?: WritableStream<StreamTargetChunk>;
}): Promise<ExportResult> {
	const { format, quality, fps, includeAudio } = options;

	try {
		const duration = calculateTotalDuration({ tracks });
		if (duration === 0) {
			return { success: false, error: "Project is empty" };
		}

		const exportFps = fps ?? settings.fps;
		const canvasSize = settings.canvasSize;

		let audioBuffer: AudioBuffer | null = null;
		if (includeAudio) {
			onProgress?.({ progress: 0.05 });
			audioBuffer = await createTimelineAudioBuffer({
				tracks,
				mediaAssets,
				duration,
			});
		}

		const scene = buildScene({
			tracks,
			mediaAssets,
			duration,
			canvasSize,
			background: settings.background,
		});

		const exporter = new SceneExporter({
			width: canvasSize.width,
			height: canvasSize.height,
			fps: exportFps,
			format,
			quality,
			shouldIncludeAudio: !!includeAudio,
			audioBuffer: audioBuffer || undefined,
		});

		exporter.on("progress", (progress) => {
			const adjustedProgress = includeAudio ? 0.05 + progress * 0.95 : progress;
			onProgress?.({ progress: adjustedProgress });
		});

		let cancelled = false;
		const checkCancel = () => {
			if (onCancel?.()) {
				cancelled = true;
				exporter.cancel();
			}
		};

		const cancelInterval = setInterval(checkCancel, 100);

		try {
			if (stream) {
				const ok = await exporter.exportToStream({ rootNode: scene, stream });
				if (cancelled) {
					return { success: false, cancelled: true };
				}
				return ok
					? { success: true }
					: { success: false, error: "Export failed" };
			}

			const buffer = await exporter.export({ rootNode: scene });

			if (cancelled) {
				return { success: false, cancelled: true };
			}

			if (!buffer) {
				return { success: false, error: "Export failed to produce buffer" };
			}

			return {
				success: true,
				buffer,
			};
		} finally {
			clearInterval(cancelInterval);
		}
	} catch (error) {
		console.error("Export failed:", error);
		return {
			success: false,
			error: error instanceof Error ? error.message : "Unknown export error",
		};
	}
}
