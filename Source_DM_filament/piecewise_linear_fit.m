% Find optimal pair of lines to fit noisy data, one line on left side and one line on right side.  Separation/crossing x value is identified.
clc;	% Clear command window.
clear;	% Delete all variables.
close all;	% Close all figure windows except those created by imtool.
workspace;	% Make sure the workspace panel is showing.
fontSize = 18;
markerSize = 20;

% Define number of points.
numPoints = 100;
midIndex = round(numPoints/2);

% Define equation 1.
m1 = 4;
b1 = 0;
x = sort(rand(1, numPoints));
y = zeros(1, numPoints); % Preallocate and set up length to be the same as x.
% Make first 50 points from line 1.
y(1:midIndex) = m1 .* x(1:midIndex) + b1;
% Get end point of line 1.
xc = x(midIndex);
yc = m1 * xc + b1;

% Define equation 2 so that it meets up with the end of equation 1.
m2 = 1;
b2 = yc - m2 * xc;
% Make second 50 points from line 2.
y(midIndex + 1 : end) = m2 .* x(midIndex + 1 : end) + b2;
% Add noise to y values.
noiseAmplitude = 0.5;
yNoisy = y + noiseAmplitude * rand(1, numPoints);

% Plot lines.
subplot(2, 2, 1);
plot(x, yNoisy, 'b.', 'MarkerSize', markerSize);
grid on;
xlabel('x', 'FontSize', fontSize);
ylabel('y', 'FontSize', fontSize);
title('Noisy Y vs. X data', 'FontSize', fontSize);
% Enlarge figure to full screen.
set(gcf, 'Units', 'Normalized', 'OuterPosition', [0, 0.04, 1, 0.96]);

% Assume the crossing point will be somewhere in the middle half of the points.
% Fit a line through the right and left parts and get the slopes.
% Keep the point where the slope difference is greatest.
index1 = round(0.25 * numPoints); % 25% of the way through.
index2 = round(0.75 * numPoints); % 75% of the way through.
% In other words, assume that we need at least 25 percent of the points to make a good estimate of the line.
% Obviously if we took only 2 or 3 points, then the slope could vary quite dramatically,
% so let's use at least 25% of the points to make sure we don't get crazy slopes.
% Initialize structure array
for k = 1 : numPoints
	lineData(k).slopeDifferences = 0;
	lineData(k).line1 = [0,0];
	lineData(k).line2 = [0,0];
end
for k = index1 : index2
	% Get data in left side.
	x1 = x(1:k);
	y1 = yNoisy(1:k);
	% Fit a line through the left side.
	coefficients1 = polyfit(x1, y1, 1); % The slope is coefficients1(1).

	% Get data in right side.
	x2 = x(k+1:end);
	y2 = yNoisy(k+1:end);
	% Fit a line through the left side.
	coefficients2 = polyfit(x2, y2, 1); % The slope is coefficients2(1).
	
	% Compute difference in slopes, and store in structure array along with line equation coefficients.
	lineData(k).slopeDifferences = abs(coefficients1(1) - coefficients2(1));
	lineData(k).line1 = coefficients1;
	lineData(k).line2 = coefficients2;
end
% Find index for which slope difference is greatest.
slopeDifferences = [lineData.slopeDifferences]; % Extract from structure array into double vector of slope differences only
% slope1s = struct2table(lineData.line1); % Extract from structure array into double vector of slopes only
% slope2s = [lineData.line2(1)]; % Extract from structure array into double vector of slopes only
[maxSlopeDiff, indexOfMaxSlopeDiff] = max(slopeDifferences)

% Plot slope differences.
subplot(2, 2, 2);
plot(slopeDifferences, 'b.', 'MarkerSize', markerSize);
grid on;
caption = sprintf('Slope Differences Maximum at Index = %d, x value = %.2f', indexOfMaxSlopeDiff, x(indexOfMaxSlopeDiff));
title(caption, 'FontSize', fontSize);

% Mark it with a red line.
line([indexOfMaxSlopeDiff, indexOfMaxSlopeDiff], [0, maxSlopeDiff], 'Color', 'r', 'LineWidth', 2);

% Show everything together all on one plot.
% Plot lines.
subplot(2, 2, 3:4);
plot(x, yNoisy, 'b.', 'MarkerSize', markerSize);
grid on;
xlabel('x', 'FontSize', fontSize);
ylabel('y', 'FontSize', fontSize);
hold on;

% Use the equation of line1 to get fitted/regressed y1 values.
slope1 = lineData(indexOfMaxSlopeDiff).line1(1);
intercept1 = lineData(indexOfMaxSlopeDiff).line1(2);
y1Fitted = slope1 * x + intercept1;
% Plot line 1 over/through data.
plot(x, y1Fitted, 'r-', 'LineWidth', 2);

% Use the equation of line2 to get fitted/regressed y2 values.
slope2 = lineData(indexOfMaxSlopeDiff).line2(1);
intercept2 = lineData(indexOfMaxSlopeDiff).line2(2);
y2Fitted = slope2 * x + intercept2;
% Plot line 2 over/through data.
plot(x, y2Fitted, 'r-', 'LineWidth', 2);
% Mark crossing with a magenta line.
xc = (intercept2 - intercept1) / (slope1 - slope2);
line([xc, xc], [0, y2Fitted(indexOfMaxSlopeDiff)], 'Color', 'm', 'LineWidth', 2);
title('Data with left and right lines overlaid', 'FontSize', fontSize);
message1 = sprintf('Left  Equation: y = %.3f * x + %.3f', slope1, intercept1);
message2 = sprintf('Right Equation: y = %.3f * x + %.3f', slope2, intercept2);
message = sprintf('%s\n%s', message1, message2);
fprintf('%s\n', message);
text(.01, 3, message, 'Color', 'r', 'FontSize', 15, 'FontWeight', 'bold');
uiwait(helpdlg(message));



