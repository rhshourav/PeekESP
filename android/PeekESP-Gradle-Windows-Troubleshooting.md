# Troubleshooting: Gradle `R.jar` File-Lock Error on Windows

## Symptom

Running the Android Gradle tests fails during resource processing:

```text
> Task :app:processDebugResources FAILED

Execution failed for task ':app:processDebugResources'.
java.io.IOException: Couldn't delete
D:\GITHUB\PeekESP\android\app\build\intermediates\compile_and_runtime_not_namespaced_r_class_jar\debug\processDebugResources\R.jar
```

## Likely cause

Windows is preventing Gradle from deleting or replacing `R.jar` because another process has the file open. Common causes include:

- A Gradle or Kotlin daemon left over from a previous build.
- Android Studio building, syncing, or indexing the project at the same time.
- Antivirus or endpoint protection scanning generated build files.
- Another process accessing files under `app\build`.

This error alone does **not** indicate a source-code defect. Resource processing failed before the test run could provide a reliable result.

## Fix it in order

Run the following commands in PowerShell from:

```powershell
D:\GITHUB\PeekESP\android
```

### 1. Stop Gradle daemons

```powershell
.\gradlew.bat --stop
```

### 2. Stop competing project activity

- Stop any active build or Gradle sync in Android Studio.
- Close Android Studio temporarily if the file remains locked.
- Close File Explorer windows currently browsing the project's `app\build` directory.
- Ensure no other terminal is building the same project.

### 3. Stop stray Java processes only if necessary

**Warning:** This terminates every process named `java`, which may include Java applications unrelated to Gradle. Save your work and close other Java-based tools first.

```powershell
Stop-Process -Name java -Force -ErrorAction SilentlyContinue
```

If you cannot safely terminate all Java processes, skip this step and restart Windows instead.

### 4. Remove generated build output

From the `android` directory:

```powershell
Remove-Item -LiteralPath .\app\build -Recurse -Force
```

This removes generated build files, not your application source code. Gradle will recreate the directory.

If PowerShell reports that a file is still in use, do not repeatedly force-delete it. Restart Windows, then run this command before opening Android Studio or starting another build.

### 5. Run the tests again

```powershell
.\gradlew.bat test --no-daemon
```

`--no-daemon` avoids reusing a persistent Gradle daemon for this invocation; it does not guarantee that other processes cannot lock files.

## If the error keeps returning

1. **Identify the locking process.** Use Microsoft Sysinternals Process Explorer or Handle to search for `R.jar` or the `processDebugResources` directory. Close or stop the identified process safely.
2. **Check antivirus activity.** Review Windows Security or your organization's endpoint-protection logs to see whether scanning coincides with the failure.
3. **Consider a build-directory exclusion only if justified.** If your security policy permits it and logs confirm repeated interference, ask your administrator about excluding the specific generated build directory, such as `D:\GITHUB\PeekESP\android\app\build`. Avoid excluding the entire repository unless there is a clear need; exclusions reduce scanning protection.
4. **Avoid concurrent builds.** Do not run Gradle builds from a terminal while Android Studio is syncing or building the same project.

## Verify the outcome

If the command completes successfully, review the test summary to confirm which tests actually ran. If the build fails again, capture the detailed error:

```powershell
.\gradlew.bat test --stacktrace
```

For more logging:

```powershell
.\gradlew.bat test --info
```

Share the first relevant exception and the final `BUILD FAILED` section when requesting further help.

## Important notes

- `./gradlew test` and `.\gradlew.bat test` are both commonly usable from PowerShell, but the `.bat` form makes the Windows wrapper explicit.
- `test` generally runs JVM unit tests for configured variants; it does not necessarily run Android instrumentation tests on a device or emulator.
- A successful retry confirms that this build completed, not that every application behavior is correct.
