package com.example.chessassiststreamer

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.SurfaceTexture
import android.graphics.YuvImage
import android.hardware.camera2.*
import android.hardware.camera2.params.StreamConfigurationMap
import android.media.Image
import android.media.ImageReader
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.wifi.WifiManager
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import android.util.Range
import android.util.Size
import android.view.Surface
import android.view.TextureView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.google.android.material.button.MaterialButton
import com.google.android.material.button.MaterialButtonToggleGroup
import com.google.android.material.textfield.MaterialAutoCompleteTextView
import com.google.android.material.textfield.TextInputEditText
import kotlinx.coroutines.*
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Socket
import java.nio.ByteBuffer
import java.text.DecimalFormat
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.abs

class MainActivity : AppCompatActivity() {

    private lateinit var cameraManager: CameraManager
    private var cameraDevice: CameraDevice? = null
    private var captureSession: CameraCaptureSession? = null
    private var imageReader: ImageReader? = null
    private var previewSurface: Surface? = null

    private lateinit var textureView: TextureView
    private lateinit var ipEditText: TextInputEditText
    private lateinit var portEditText: TextInputEditText
    private lateinit var startStopButton: MaterialButton
    private lateinit var statusIndicator: android.view.View
    private lateinit var statusText: TextView
    private lateinit var cameraToggle: MaterialButtonToggleGroup
    private lateinit var frontCameraButton: MaterialButton
    private lateinit var rearCameraButton: MaterialButton
    private lateinit var resolutionSpinner: MaterialAutoCompleteTextView

    private val cameraThread = HandlerThread("CameraThread")
    private lateinit var cameraHandler: Handler

    private val streamingScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var streamingJob: Job? = null

    // Buffer pool for JPEG compression to reduce allocations
    private val outputStreamPool = object {
        private val pool = ArrayDeque<ByteArrayOutputStream>(3)
        
        @Synchronized
        fun acquire(): ByteArrayOutputStream {
            return if (pool.isNotEmpty()) {
                pool.removeFirst().apply { reset() }
            } else {
                ByteArrayOutputStream(256 * 1024) // Pre-allocate 256KB
            }
        }
        
        @Synchronized
        fun release(stream: ByteArrayOutputStream) {
            if (pool.size < 3) {
                stream.reset()
                pool.addLast(stream)
            }
        }
    }

    private var currentSocket: Socket? = null
    private var outputStream: DataOutputStream? = null
    private var inputStream: java.io.DataInputStream? = null
    private val sendMutex = Mutex()

    private val isStreaming = AtomicBoolean(false)
    private var selectedCameraId: String? = null
    private var selectedSize: Size = Size(1280, 720)
    
    // Camera control parameters
    private var currentZoom: Float = 1.0f
    private var currentExposure: Int = 0
    private var currentFocus: Float = 0.5f
    private var controlListenerJob: Job? = null
    private var captureRequestBuilder: CaptureRequest.Builder? = null

    private val fpsFormat = DecimalFormat("0.0")
    private var frameCounter = 0
    private var lastFpsTimestamp = 0L
    private var lastFrameTimestamp = 0L
    private val minFrameIntervalMs = 33L // ~30 FPS max to prevent overwhelming network

    private val requestPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { permissions ->
        val granted = permissions.entries.all { it.value }
        if (granted) {
            startStreaming()
        } else {
            Toast.makeText(this, "Camera and network permissions are required", Toast.LENGTH_LONG).show()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        cameraThread.start()
        cameraHandler = Handler(cameraThread.looper)

        cameraManager = getSystemService(Context.CAMERA_SERVICE) as CameraManager

        bindViews()
        configureUi()
        populateAutoFields()
    }

    override fun onDestroy() {
        super.onDestroy()
        stopStreaming()
        cameraThread.quitSafely()
    }

    private fun bindViews() {
        textureView = findViewById(R.id.previewTextureView)
        ipEditText = findViewById(R.id.ipAddressEditText)
        portEditText = findViewById(R.id.portEditText)
        startStopButton = findViewById(R.id.startStopButton)
        statusIndicator = findViewById(R.id.statusIndicator)
        statusText = findViewById(R.id.statusTextView)
        cameraToggle = findViewById(R.id.cameraToggleGroup)
        frontCameraButton = findViewById(R.id.frontCameraButton)
        rearCameraButton = findViewById(R.id.rearCameraButton)
        resolutionSpinner = findViewById(R.id.resolutionSpinner)
    }

    private fun configureUi() {
        startStopButton.setOnClickListener {
            if (isStreaming.get()) {
                stopStreaming()
            } else {
                ensurePermissionsAndStart()
            }
        }

        cameraToggle.addOnButtonCheckedListener { _, checkedId, isChecked ->
            if (isChecked) {
                selectedCameraId = when (checkedId) {
                    R.id.frontCameraButton -> getCameraId(CameraCharacteristics.LENS_FACING_FRONT)
                    else -> getCameraId(CameraCharacteristics.LENS_FACING_BACK)
                }
                if (isStreaming.get()) {
                    restartCamera()
                }
            }
        }

        val resolutionItems = resources.getStringArray(R.array.resolution_labels)
        resolutionSpinner.setSimpleItems(resolutionItems)
        resolutionSpinner.setText(resolutionItems[1], false)
        resolutionSpinner.setOnItemClickListener { _, _, position, _ ->
            selectedSize = when (position) {
                0 -> Size(640, 480)
                1 -> Size(1280, 720)
                else -> Size(1920, 1080)
            }
            if (isStreaming.get()) {
                restartCamera()
            }
        }

        textureView.surfaceTextureListener = object : TextureView.SurfaceTextureListener {
            override fun onSurfaceTextureAvailable(surface: SurfaceTexture, width: Int, height: Int) {
                if (isStreaming.get()) {
                    startCameraPreview()
                }
            }

            override fun onSurfaceTextureSizeChanged(surface: SurfaceTexture, width: Int, height: Int) {}

            override fun onSurfaceTextureDestroyed(surface: SurfaceTexture): Boolean {
                previewSurface = null
                return true
            }

            override fun onSurfaceTextureUpdated(surface: SurfaceTexture) {}
        }

        rearCameraButton.isChecked = true
        selectedCameraId = getCameraId(CameraCharacteristics.LENS_FACING_BACK)
    }

    private fun populateAutoFields() {
        ipEditText.setText(getLocalIpAddress() ?: "")
        portEditText.setText("5000")
    }

    private fun ensurePermissionsAndStart() {
        val permissionsNeeded = mutableListOf<String>()
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            permissionsNeeded.add(Manifest.permission.CAMERA)
        }
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_NETWORK_STATE) != PackageManager.PERMISSION_GRANTED) {
            permissionsNeeded.add(Manifest.permission.ACCESS_NETWORK_STATE)
        }
        if (permissionsNeeded.isNotEmpty()) {
            requestPermissionLauncher.launch(permissionsNeeded.toTypedArray())
        } else {
            startStreaming()
        }
    }

    private fun startStreaming() {
        val ipAddress = ipEditText.text?.toString()?.trim()
        val portString = portEditText.text?.toString()?.trim()

        if (ipAddress.isNullOrEmpty() || portString.isNullOrEmpty()) {
            Toast.makeText(this, "Please provide IP and port", Toast.LENGTH_SHORT).show()
            return
        }

        val port = portString.toIntOrNull()
        if (port == null || port !in 1024..65535) {
            Toast.makeText(this, "Invalid port", Toast.LENGTH_SHORT).show()
            return
        }

        if (selectedCameraId == null) {
            Toast.makeText(this, "Camera not available", Toast.LENGTH_SHORT).show()
            return
        }

        isStreaming.set(true)
        updateStatusIndicator(true)
        startStopButton.text = getString(R.string.stop)

        setupNetworking(ipAddress, port)
        startCameraPreview()
    }

    private fun setupNetworking(ipAddress: String, port: Int) {
        streamingJob?.cancel()
        controlListenerJob?.cancel()
        streamingJob = streamingScope.launch {
            try {
                withContext(Dispatchers.Main) {
                    statusText.text = "Connecting..."
                }
                val socket = Socket()
                socket.tcpNoDelay = true
                socket.receiveBufferSize = 64 * 1024  // 64KB receive buffer
                socket.sendBufferSize = 512 * 1024    // 512KB send buffer for better throughput
                socket.connect(InetSocketAddress(ipAddress, port), 3000)
                currentSocket = socket
                outputStream = DataOutputStream(socket.getOutputStream())
                inputStream = java.io.DataInputStream(socket.getInputStream())

                withContext(Dispatchers.Main) {
                    Toast.makeText(this@MainActivity, "Connected to $ipAddress:$port", Toast.LENGTH_SHORT).show()
                    statusText.text = "Connected"
                }
                
                // Start listening for control commands
                startControlListener()
            } catch (e: Exception) {
                Log.e(TAG, "Failed to connect", e)
                withContext(Dispatchers.Main) {
                    Toast.makeText(this@MainActivity, "Connection failed: ${e.message}", Toast.LENGTH_LONG).show()
                    stopStreaming()
                }
            }
        }
    }

    private fun startCameraPreview() {
        val texture = textureView.surfaceTexture ?: return
        val cameraId = selectedCameraId ?: return

        cleanupCamera()

        try {
            val characteristics = cameraManager.getCameraCharacteristics(cameraId)
            val configurationMap = characteristics.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
                ?: throw IllegalStateException("Stream configuration unavailable")
            val previewSize = chooseOptimalSize(configurationMap, selectedSize)
            texture.setDefaultBufferSize(previewSize.width, previewSize.height)
            previewSurface = Surface(texture)

            imageReader = ImageReader.newInstance(previewSize.width, previewSize.height, ImageFormat.YUV_420_888, 2)
            imageReader?.setOnImageAvailableListener({ reader ->
                // Throttle frame processing to prevent overwhelming the network
                val now = System.currentTimeMillis()
                if (now - lastFrameTimestamp < minFrameIntervalMs) {
                    reader.acquireLatestImage()?.close() // Drop frame but clean up
                    return@setOnImageAvailableListener
                }
                lastFrameTimestamp = now
                
                val image = reader.acquireLatestImage() ?: return@setOnImageAvailableListener
                try {
                    val jpegBytes = image.toJpegBytes(80)
                    if (jpegBytes != null && isStreaming.get()) {
                        sendFrame(jpegBytes)
                    }
                } finally {
                    image.close() // Ensure image is always closed
                }
            }, cameraHandler)

            if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
                return
            }

            cameraManager.openCamera(cameraId, object : CameraDevice.StateCallback() {
                override fun onOpened(camera: CameraDevice) {
                    cameraDevice = camera
                    createCaptureSession()
                }

                override fun onDisconnected(camera: CameraDevice) {
                    camera.close()
                    cameraDevice = null
                    stopStreaming()
                }

                override fun onError(camera: CameraDevice, error: Int) {
                    camera.close()
                    cameraDevice = null
                    stopStreaming()
                }
            }, cameraHandler)

        } catch (e: Exception) {
            Log.e(TAG, "Failed to start camera", e)
            Toast.makeText(this, "Camera error: ${e.message}", Toast.LENGTH_LONG).show()
            stopStreaming()
        }
    }

    private fun createCaptureSession() {
        val camera = cameraDevice ?: return
        val surfaces = mutableListOf<Surface>()

        previewSurface?.let { surfaces.add(it) }
        imageReader?.surface?.let { surfaces.add(it) }

        camera.createCaptureSession(surfaces, object : CameraCaptureSession.StateCallback() {
            override fun onConfigured(session: CameraCaptureSession) {
                captureSession = session
                val requestBuilder = camera.createCaptureRequest(CameraDevice.TEMPLATE_RECORD)
                previewSurface?.let { requestBuilder.addTarget(it) }
                imageReader?.surface?.let { requestBuilder.addTarget(it) }

                requestBuilder.set(CaptureRequest.CONTROL_MODE, CameraMetadata.CONTROL_MODE_AUTO)
                requestBuilder.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, Range(15, 30))
                
                // Store request builder for dynamic updates
                captureRequestBuilder = requestBuilder
                applyCameraControls(requestBuilder)

                session.setRepeatingRequest(requestBuilder.build(), null, cameraHandler)
            }

            override fun onConfigureFailed(session: CameraCaptureSession) {
                Toast.makeText(this@MainActivity, "Capture session failed", Toast.LENGTH_LONG).show()
                stopStreaming()
            }
        }, cameraHandler)
    }

    private fun sendFrame(jpegBytes: ByteArray) {
        if (!isStreaming.get()) return
        
        // Use a coroutine with limited concurrency instead of mutex to reduce lock contention
        streamingScope.launch {
            try {
                val output = outputStream ?: return@launch
                // Write size and data in one operation to reduce lock time
                sendMutex.withLock {
                    output.writeInt(jpegBytes.size)
                    output.write(jpegBytes)
                    // Flush less frequently - only after write completes
                }
                // Flush outside lock to reduce contention
                output.flush()
                updateFps()
            } catch (e: Exception) {
                Log.e(TAG, "Sending frame failed", e)
                withContext(Dispatchers.Main) {
                    Toast.makeText(this@MainActivity, "Streaming stopped: ${e.message}", Toast.LENGTH_LONG).show()
                    stopStreaming()
                }
            }
        }
    }

    private fun updateFps() {
        frameCounter++
        // Only update FPS display after at least 30 frames to reduce UI overhead
        // Actual update happens every 1 second or more
        if (frameCounter < 30) return
        
        val now = System.currentTimeMillis()
        if (lastFpsTimestamp == 0L) {
            lastFpsTimestamp = now
            frameCounter = 0
            return
        }
        
        val elapsed = now - lastFpsTimestamp
        if (elapsed >= 1000) {
            val fps = frameCounter * 1000f / elapsed
            frameCounter = 0
            lastFpsTimestamp = now
            runOnUiThread {
                statusText.text = "Streaming ${fpsFormat.format(fps)} FPS"
            }
        }
    }

    private fun restartCamera() {
        streamingScope.launch(Dispatchers.Main) {
            stopCamera()
            if (isStreaming.get()) {
                startCameraPreview()
            }
        }
    }

    private fun stopStreaming() {
        if (!isStreaming.getAndSet(false)) return

        streamingJob?.cancel()
        streamingJob = null
        controlListenerJob?.cancel()
        controlListenerJob = null

        streamingScope.launch {
            try {
                outputStream?.flush()
            } catch (_: Exception) {
            }
            try {
                outputStream?.close()
            } catch (_: Exception) {
            }
            try {
                inputStream?.close()
            } catch (_: Exception) {
            }
            try {
                currentSocket?.close()
            } catch (_: Exception) {
            }
            outputStream = null
            inputStream = null
            currentSocket = null
        }

        stopCamera()

        runOnUiThread {
            updateStatusIndicator(false)
            statusText.text = "Idle"
            startStopButton.text = getString(R.string.start)
        }
    }

    private fun stopCamera() {
        try {
            captureSession?.close()
        } catch (_: Exception) {
        }
        try {
            cameraDevice?.close()
        } catch (_: Exception) {
        }
        try {
            imageReader?.close()
        } catch (_: Exception) {
        }
        captureSession = null
        cameraDevice = null
        imageReader = null
        previewSurface = null
    }

    private fun cleanupCamera() {
        captureSession?.close()
        captureSession = null
        imageReader?.close()
        imageReader = null
        cameraDevice?.close()
        cameraDevice = null
    }

    private fun updateStatusIndicator(active: Boolean) {
        statusIndicator.setBackgroundResource(
            if (active) R.drawable.status_indicator_on else R.drawable.status_indicator_off
        )
    }

    private fun chooseOptimalSize(map: StreamConfigurationMap, desired: Size): Size {
        val choices = map.getOutputSizes(ImageFormat.YUV_420_888)
        if (choices.isNullOrEmpty()) {
            return desired
        }
        val exact = choices.firstOrNull { it.width == desired.width && it.height == desired.height }
        if (exact != null) return exact

        var closest = choices.first()
        var minDiff = Int.MAX_VALUE
        for (size in choices) {
            val diff = abs(size.width - desired.width) + abs(size.height - desired.height)
            if (diff < minDiff) {
                minDiff = diff
                closest = size
            }
        }
        return closest
    }

    private fun getCameraId(lensFacing: Int): String? {
        for (id in cameraManager.cameraIdList) {
            val characteristics = cameraManager.getCameraCharacteristics(id)
            val facing = characteristics.get(CameraCharacteristics.LENS_FACING)
            if (facing == lensFacing) {
                return id
            }
        }
        return null
    }

    private fun Image.toJpegBytes(quality: Int): ByteArray? {
        if (format != ImageFormat.YUV_420_888) {
            return null
        }
        
        // Get buffer pool instance
        val outputStream = outputStreamPool.acquire()
        
        return try {
            val yBuffer: ByteBuffer = planes[0].buffer
            val uBuffer: ByteBuffer = planes[1].buffer
            val vBuffer: ByteBuffer = planes[2].buffer

            val ySize = yBuffer.remaining()
            val uSize = uBuffer.remaining()
            val vSize = vBuffer.remaining()

            val nv21 = ByteArray(ySize + uSize + vSize)
            yBuffer.get(nv21, 0, ySize)
            
            val uvStride = planes[1].pixelStride
            if (uvStride == 2) {
                // Interleaved UV planes - more efficient processing
                var uvIndex = ySize
                var uIndex = 0
                var vIndex = 0
                // Ensure we have space for both V and U bytes
                while (uIndex < uSize && vIndex < vSize && uvIndex < nv21.size - 1) {
                    nv21[uvIndex++] = vBuffer.get(vIndex++)
                    nv21[uvIndex++] = uBuffer.get(uIndex++)
                }
            } else {
                // Semi-planar format
                var position = ySize
                vBuffer.get(nv21, position, vSize)
                position += vSize
                uBuffer.get(nv21, position, uSize)
            }

            val yuvImage = YuvImage(nv21, ImageFormat.NV21, width, height, null)
            yuvImage.compressToJpeg(Rect(0, 0, width, height), quality, outputStream)
            outputStream.toByteArray()
        } catch (e: Exception) {
            Log.e(TAG, "Compression error", e)
            null
        } finally {
            outputStreamPool.release(outputStream)
        }
    }

    private fun getLocalIpAddress(): String? {
        try {
            val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            val ip = wm.connectionInfo.ipAddress
            if (ip != 0) {
                return String.format(
                    "%d.%d.%d.%d",
                    ip and 0xff,
                    ip shr 8 and 0xff,
                    ip shr 16 and 0xff,
                    ip shr 24 and 0xff
                )
            }
        } catch (_: SecurityException) {
        } catch (_: Exception) {
        }

        try {
            val connectivityManager = getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
            val activeNetwork = connectivityManager.activeNetwork ?: return null
            val linkProperties: LinkProperties = connectivityManager.getLinkProperties(activeNetwork) ?: return null
            for (address in linkProperties.linkAddresses) {
                val hostAddress = address.address.hostAddress
                if (!hostAddress.isNullOrBlank() && !hostAddress.contains(':')) {
                    return hostAddress
                }
            }
        } catch (_: Exception) {
        }

        return try {
            InetAddress.getLocalHost().hostAddress
        } catch (e: Exception) {
            Log.e(TAG, "IP detection failed", e)
            null
        }
    }
    
    private fun startControlListener() {
        controlListenerJob?.cancel()
        controlListenerJob = streamingScope.launch {
            try {
                val input = inputStream ?: return@launch
                val buffer = ByteArray(256) // Buffer for reading commands
                var partialLine = StringBuilder()
                
                while (isStreaming.get() && isActive) {
                    try {
                        val available = input.available()
                        if (available > 0) {
                            val readSize = minOf(available, buffer.size)
                            val bytesRead = input.read(buffer, 0, readSize)
                            if (bytesRead <= 0) break
                            
                            val chunk = String(buffer, 0, bytesRead, Charsets.UTF_8)
                            partialLine.append(chunk)
                            
                            // Process complete lines
                            var newlineIndex = partialLine.indexOf('\n')
                            while (newlineIndex >= 0) {
                                val command = partialLine.substring(0, newlineIndex).trim()
                                if (command.isNotEmpty()) {
                                    handleControlCommand(command)
                                }
                                partialLine.delete(0, newlineIndex + 1)
                                newlineIndex = partialLine.indexOf('\n')
                            }
                        } else {
                            // Wait a bit before checking again to avoid busy-waiting
                            delay(10)
                        }
                    } catch (e: Exception) {
                        if (isStreaming.get()) {
                            Log.e(TAG, "Control listener error", e)
                        }
                        break
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Control listener failed", e)
            }
        }
    }
    
    private fun handleControlCommand(command: String) {
        // Optimize parsing - use indexOf instead of split to avoid array allocation
        val colonIndex = command.indexOf(':')
        if (colonIndex <= 0 || colonIndex >= command.length - 1) return
        
        val commandType = command.substring(0, colonIndex)
        val valueStr = command.substring(colonIndex + 1)
        
        when (commandType) {
            "ZOOM" -> {
                currentZoom = valueStr.toFloatOrNull()?.coerceIn(1.0f, 10.0f) ?: currentZoom
                updateCameraSettings()
            }
            "EXPOSURE" -> {
                currentExposure = valueStr.toIntOrNull()?.coerceIn(-12, 12) ?: currentExposure
                updateCameraSettings()
            }
            "FOCUS" -> {
                currentFocus = valueStr.toFloatOrNull()?.coerceIn(0.0f, 1.0f) ?: currentFocus
                updateCameraSettings()
            }
        }
    }
    
    private fun applyCameraControls(requestBuilder: CaptureRequest.Builder) {
        try {
            val cameraId = selectedCameraId ?: return
            val characteristics = cameraManager.getCameraCharacteristics(cameraId)
            
            // Apply zoom
            val maxZoom = characteristics.get(CameraCharacteristics.SCALER_AVAILABLE_MAX_DIGITAL_ZOOM) ?: 1.0f
            val zoomRatio = currentZoom.coerceIn(1.0f, maxZoom)
            val cropRegion = characteristics.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE)
            if (cropRegion != null && zoomRatio > 1.0f) {
                val centerX = cropRegion.width() / 2
                val centerY = cropRegion.height() / 2
                val deltaX = (cropRegion.width() / (2 * zoomRatio)).toInt()
                val deltaY = (cropRegion.height() / (2 * zoomRatio)).toInt()
                val zoomRect = Rect(centerX - deltaX, centerY - deltaY, centerX + deltaX, centerY + deltaY)
                requestBuilder.set(CaptureRequest.SCALER_CROP_REGION, zoomRect)
            }
            
            // Apply exposure compensation
            val exposureRange = characteristics.get(CameraCharacteristics.CONTROL_AE_COMPENSATION_RANGE)
            if (exposureRange != null) {
                val exposure = currentExposure.coerceIn(exposureRange.lower, exposureRange.upper)
                requestBuilder.set(CaptureRequest.CONTROL_AE_EXPOSURE_COMPENSATION, exposure)
            }
            
            // Apply manual focus if supported
            val minFocusDistance = characteristics.get(CameraCharacteristics.LENS_INFO_MINIMUM_FOCUS_DISTANCE) ?: 0f
            if (minFocusDistance > 0f) {
                requestBuilder.set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_OFF)
                requestBuilder.set(CaptureRequest.LENS_FOCUS_DISTANCE, currentFocus * minFocusDistance)
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to apply camera controls", e)
        }
    }
    
    private fun updateCameraSettings() {
        cameraHandler.post {
            try {
                val session = captureSession ?: return@post
                val builder = captureRequestBuilder ?: return@post
                
                applyCameraControls(builder)
                session.setRepeatingRequest(builder.build(), null, cameraHandler)
            } catch (e: Exception) {
                Log.e(TAG, "Failed to update camera settings", e)
            }
        }
    }

    companion object {
        private const val TAG = "ChessAssistStreamer"
    }
}
