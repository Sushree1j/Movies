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

    private var currentSocket: Socket? = null
    private var outputStream: DataOutputStream? = null
    private val sendMutex = Mutex()

    private val isStreaming = AtomicBoolean(false)
    private var selectedCameraId: String? = null
    private var selectedSize: Size = Size(1280, 720)

    private val fpsFormat = DecimalFormat("0.0")
    private var frameCounter = 0
    private var lastFpsTimestamp = 0L

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
        streamingJob = streamingScope.launch {
            try {
                withContext(Dispatchers.Main) {
                    statusText.text = "Connecting..."
                }
                val socket = Socket()
                socket.tcpNoDelay = true
                socket.connect(InetSocketAddress(ipAddress, port), 3000)
                currentSocket = socket
                outputStream = DataOutputStream(socket.getOutputStream())

                withContext(Dispatchers.Main) {
                    Toast.makeText(this@MainActivity, "Connected to $ipAddress:$port", Toast.LENGTH_SHORT).show()
                    statusText.text = "Connected"
                }
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
                val image = reader.acquireLatestImage() ?: return@setOnImageAvailableListener
                val jpegBytes = image.toJpegBytes(80)
                image.close()
                if (jpegBytes != null && isStreaming.get()) {
                    sendFrame(jpegBytes)
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
        streamingScope.launch {
            try {
                val output = outputStream ?: return@launch
                sendMutex.withLock {
                    output.writeInt(jpegBytes.size)
                    output.write(jpegBytes)
                    output.flush()
                }
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
        val now = System.currentTimeMillis()
        if (lastFpsTimestamp == 0L) {
            lastFpsTimestamp = now
        }
        frameCounter++
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
                currentSocket?.close()
            } catch (_: Exception) {
            }
            outputStream = null
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
            val uBytes = ByteArray(uSize)
            val vBytes = ByteArray(vSize)
            uBuffer.get(uBytes)
            vBuffer.get(vBytes)
            var uvIndex = ySize
            var uIndex = 0
            var vIndex = 0
            while (uIndex < uBytes.size && vIndex < vBytes.size && uvIndex < nv21.size) {
                nv21[uvIndex++] = vBytes[vIndex++]
                nv21[uvIndex++] = uBytes[uIndex++]
            }
        } else {
            var position = ySize
            vBuffer.get(nv21, position, vSize)
            position += vSize
            uBuffer.get(nv21, position, uSize)
        }

        val yuvImage = YuvImage(nv21, ImageFormat.NV21, width, height, null)
        val outputStream = ByteArrayOutputStream()
        return try {
            yuvImage.compressToJpeg(Rect(0, 0, width, height), quality, outputStream)
            outputStream.toByteArray()
        } catch (e: Exception) {
            Log.e(TAG, "Compression error", e)
            null
        } finally {
            outputStream.close()
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

    companion object {
        private const val TAG = "ChessAssistStreamer"
    }
}
