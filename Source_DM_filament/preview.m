function RecMov = preview(V, Path, vidOn, vidSaveDir, fileNameDate, ds)

close all

pos_x = 2; pos_y = 10;   % position of the figure,[cm]
margin_vert_u = 0.5;  margin_vert_d = 0.5;  margin_horz_l = 0.5;  margin_horz_r = 0.5; % margin of the figure, [cm]

subfig_num = 2;
% size of each subplot, [mm]
fig_width_1 = 18.4 - (margin_horz_l + margin_horz_r);	
fig_height_1 = 4; 
fig_width_2 = 6;
fig_height_2 = fig_height_1;

fig_size_all = [fig_width_1 + fig_width_2 + margin_horz_l*2 + margin_horz_r*2, fig_height_1 + margin_vert_u + margin_vert_d];

ax_pos = [
        margin_horz_l, margin_vert_d, fig_width_1, fig_height_1;                            % ax_pos for subfig 1
        margin_horz_l*2+fig_width_1+margin_horz_r, margin_vert_d, fig_width_2, fig_height_2   % ax_pos for subfig 2
        ];
    
fig_size_all = fig_size_all * 2;
ax_pos = ax_pos*2;

% Figure setup
fontname = 'Microsoft Sans Serif';
set(0,'defaultaxesfontname',fontname);
set(0,'defaulttextfontname',fontname);
fontsize = 9;
set(0,'defaultaxesfontsize',fontsize);
set(0,'defaulttextfontsize',fontsize);
set(0,'fixedwidthfontname','times');

hfig = figure(1);
set(gcf,'renderer','painters');
set(gcf,'units','centimeters');
set(gcf,'position',[pos_x pos_y fig_size_all]);
set(gcf,'PaperUnits','centimeters');
set(gcf,'PaperSize', fig_size_all);
set(gcf,'PaperPositionMode', 'manual');
set(gcf,'PaperPosition',[0 0 fig_size_all]);
set(gcf,'Toolbar','none');

% Define subplots
hax = zeros(1,subfig_num);
for idx = 1:subfig_num
    hax(idx) = axes('Units','centimeters','activepositionproperty','outerposition');
    set(hax(idx),'position', ax_pos(idx,:));
    try
        disableDefaultInteractivity(hax(idx));
    catch
    end
    try
        if isprop(hax(idx), 'Toolbar')
            hax(idx).Toolbar.Visible = 'off';
        end
    catch
    end
end

% Set details of subplot 1

axes(hax(1));
ht = title('Printing Preview (Side)','Units','centimeters');
ht_pos = get(ht,'position');
ht_pos(2) = ht_pos(2) + 0.4;    % Move subtitle to avoid overlap with plot's thick line
set(ht,'position',ht_pos);
xlabel('Filament Length')
ylabel('Layer')
ax = gca;
ax.XLim = [ 0 max(V(:,3))*ds ];
ax.YLim = [0 14];
ax.XAxis.Exponent = 0;
hold on         % Add 'hold on' before calling another axes


% Set details of subplot 2
axes(hax(2));
ht2 = title('Printing Preview (Top)','Units','centimeters');
ht2_pos = get(ht2,'position');
ht2_pos(2) = ht2_pos(2) + 0.4;    % Move subtitle to align with subtitle 1
set(ht2,'position',ht2_pos);
xlabel('X')
ylabel('Y')
view([0,90]); % top view
ax = gca;
ax.DataAspectRatioMode = 'manual';
ax.DataAspectRatio = [1 1 0.04];
ax.XLim = [0 250];
ax.YLim = [0 200];
ax.YTick = 0:50:200;
ax.ZLim = [-3 3];
hold on

len = size(V,1); % length of V and path is equal 
if vidOn == "on"
    fprintf('Creating Video...\n')    
    RecMov = VideoWriter(strcat(vidSaveDir,'\',fileNameDate,'_mod.mp4'), 'MPEG-4');
    RecMov.FrameRate = 0.5;
    open(RecMov);    
    prevMat = V(1,1);
    for i=1:len
        curMat = V(i,1);
        if curMat ~= prevMat
            frame = getframe(gcf);
            writeVideo(RecMov,frame);      
            figName = strcat(vidSaveDir,'\',fileNameDate,'_mod_',num2str(i));
            %export_fig(figName, '-transparent', '-png');
            prevMat = curMat;            
        end
        % Plot on subplot 1: side view
        plot(hax(1), (V(i,2):1:V(i,3))*ds,repmat( V(i,4), 1, length(V(i,2):1:V(i,3)) ),'color', mn2c(V(i,1)),'linewidth',12);
        
        % Plot on subplot 2: Top view
        ii = ( 1:1:size( Path{i,1}, 1 ) )';
        X = Path{i,1}(ii,1);
        Y = Path{i,1}(ii,2);
        Z = Path{i,2}(1);
        plot3(hax(2), X,Y,repmat(Z,ii(end),1),'-','color', mn2c(curMat),'linewidth',1);    
        if i==len
            frame = getframe(gcf);
            writeVideo(RecMov,frame);
            figName = strcat(vidSaveDir,'\',fileNameDate,'_mod_',num2str(i));
           %export_fig(figName, '-transparent', '-png');
        end
    end      
    close(RecMov);
    fprintf('Video Saved \n')
    hold off

else
    RecMov = [];
    for i=1:len
        curMat = V(i,1);
        % Plot on subplot 1: side view
        plot(hax(1), (V(i,2):1:V(i,3))*ds,repmat( V(i,4), 1, length(V(i,2):1:V(i,3)) ),'color', mn2c(V(i,1)),'linewidth',12);

        % Plot on subplot 2: Top view
%         ii = ( 1:1:size( Path{i,1}, 1 ) )';
%         X = Path{i,1}(ii,1);
%         Y = Path{i,1}(ii,2);
%         Z = Path{i,2}(1);
%         plot3(hax(2), X,Y,repmat(Z,ii(end),1),'-','color', mn2c(curMat),'linewidth',1);        
    end 
    hold off
    figName = strcat(vidSaveDir,'\',fileNameDate,'_mod_',num2str(i));
    %export_fig(figName, '-transparent', '-png');
end

fullPreviewPath = strcat(vidSaveDir,'\',fileNameDate,'_preview.png');
sidePreviewPath = strcat(vidSaveDir,'\',fileNameDate,'_side_view.png');
topPreviewPath = strcat(vidSaveDir,'\',fileNameDate,'_top_view.png');

try
    exportgraphics(hfig, fullPreviewPath, 'Resolution', 300);
    exportgraphics(hax(1), sidePreviewPath, 'Resolution', 300);
    exportgraphics(hax(2), topPreviewPath, 'Resolution', 300);
catch
    saveas(hfig, fullPreviewPath);
    saveas(hax(1), sidePreviewPath);
    saveas(hax(2), topPreviewPath);
end

fprintf('Figure Saved \n')
    
    


