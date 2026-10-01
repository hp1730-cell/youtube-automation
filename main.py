for audio_path in audio_paths:
    concat_lines.append(
        "file '{}'\n".format(
            audio_path.resolve()
        )
    )
